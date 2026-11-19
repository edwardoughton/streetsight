"""Decide whether a downloaded image shows the target.

A verifier is any callable  verifier(image_path, context) -> dict  with at least
  visible: bool | None   (None = could not decide)
  confidence: float 0-1
  reason: str
context holds target, distance_m, heading, pitch, fov, visible_above_m, target_height_m, and optionally
source, target_notes and target_offset_deg (see prompt()).
"""
import os
import base64
import json
from pathlib import Path

DEFAULT_MODEL = 'claude-opus-5-5'

SCHEMA = {
    'type': 'object',
    'properties': {
        'visible': {'type': 'boolean'},
        'confidence': {'type': 'number'},
        'position': {'type': 'string', 'enum': ['left', 'centre', 'right', 'not_visible']},
        'reason': {'type': 'string'},
    },
    'required': ['visible', 'confidence', 'position', 'reason'],
    'additionalProperties': False,
}

PROMPT = """This is a {source} image. {aim}
The {target} is about {distance_m:.0f} m from the camera and about {target_height_m:.0f} m tall; buildings or
trees may hide its lower part.

Is the {target} itself clearly visible in this image? Count it as visible only if you can see the structure
itself, not merely something nearby that might belong to it. A clearly identifiable partial view (for example
the upper part above trees) counts as visible. Do not count similar-looking objects of another kind as the
{target}.{notes}

Reply with: visible (true/false), confidence (0 to 1), position of the target in the frame
(left, centre, right, or not_visible), and a one-sentence reason."""


def prompt(context):
    """Verifier prompt for one image. context: target, distance_m, heading, pitch, fov, target_height_m, and
    optionally source (default "Google Street View"), target_notes (extra guidance appended to the question)
    and target_offset_deg (for images that could not be aimed: where the target should be, + = right)."""
    c = {'source': 'Google Street View', 'target_notes': '', **context}
    off = c.get('target_offset_deg')
    if off is None:
        aim = (f"The view was aimed, using map coordinates, at a {c['target']} (compass heading {c['heading']:.0f} deg, "
               f"pitch {c['pitch']:.0f} deg, horizontal field of view {c['fov']:.0f} deg). If the coordinates are right "
               f"and nothing blocks the view, the {c['target']} should appear near the horizontal centre of the frame.")
    else:
        side = 'right' if off >= 0 else 'left'
        aim = (f"The camera faced compass heading {c['heading']:.0f} deg (horizontal field of view about "
               f"{c['fov']:.0f} deg). From the map coordinates, a {c['target']} should be about {abs(off):.0f} deg "
               f"to the {side} of the image centre.")
    notes = f" {c['target_notes'].strip()}" if str(c['target_notes'] or '').strip() else ''
    return PROMPT.format(aim=aim, notes=notes, **{k: v for k, v in c.items() if k not in ('aim', 'notes')})


class ClaudeVerifier:
    """Vision check with Claude. Needs `pip install anthropic` and ANTHROPIC_API_KEY (or `ant auth login`)."""

    def __init__(self, model=DEFAULT_MODEL, effort='medium', client=None):
        try:
            import anthropic
        except ImportError as exc:
            raise ImportError('The Claude verifier needs the anthropic package: pip install "streetsight[claude]"') from exc
        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model, self.effort = model, effort

    def __call__(self, image_path, context):
        data = base64.standard_b64encode(Path(image_path).read_bytes()).decode()
        a = self._anthropic
        try:
            # Server-side refusal fallback: on a policy decline the API retries on a fallback model.
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                betas=['server-side-fallback-2026-07-01'],
                fallbacks='default',
                output_config={'effort': self.effort, 'format': {'type': 'json_schema', 'schema': SCHEMA}},
                messages=[{'role': 'user', 'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': data}},
                    {'type': 'text', 'text': prompt(context)},
                ]}],
            )
        except (a.AuthenticationError, a.PermissionDeniedError, a.NotFoundError, a.BadRequestError):
            raise  # configuration problems: stop the run rather than silently skipping every candidate
        except (a.RateLimitError, a.APIStatusError, a.APIConnectionError) as exc:
            return {'visible': None, 'confidence': 0.0, 'reason': f'verifier error: {type(exc).__name__}'}
        if response.stop_reason == 'refusal':
            return {'visible': None, 'confidence': 0.0, 'reason': 'model declined to answer'}
        text = next((b.text for b in response.content if b.type == 'text'), '')
        try:
            out = json.loads(text)
        except json.JSONDecodeError:
            return {'visible': None, 'confidence': 0.0, 'reason': f'unparseable verifier output (stop: {response.stop_reason})'}
        out['verifier_model'] = response.model
        return out


class ReviewFileVerifier:
    """Verdicts from a review CSV with columns image, visible, confidence, reason (one row per image file).

    `image` matches the image file name (e.g. rank01_27m_h208_<pano>.jpg) or its run-relative path.
    Images not in the file count as undecided, so find_view moves on to the next candidate. This lets a
    person (or any offline checker) review images between runs: run with n_candidates=1, fill in the
    CSV, then rerun with n_candidates=2. Rank 1 is reused, and rank 2 is downloaded only where rank 1
    was not confirmed.
    """

    def __init__(self, path):
        import csv
        self.path = Path(path)
        self.model = f'review_file:{self.path.name}'
        self.rows = {}
        if self.path.is_file():
            with open(self.path, newline='', encoding='utf-8-sig') as f:
                for r in csv.DictReader(f):
                    self.rows[Path(r['image'].replace(chr(92), '/')).name] = r

    def __call__(self, image_path, context):
        r = self.rows.get(Path(image_path).name)
        if not r:
            return {'visible': None, 'confidence': None, 'reason': 'not yet reviewed'}
        vis = str(r.get('visible', '')).strip().lower()
        return {'visible': True if vis in ('1', 'true', 'yes', 'y') else False if vis in ('0', 'false', 'no', 'n') else None,
                'confidence': float(r['confidence']) if str(r.get('confidence', '')).strip() else None,
                'reason': r.get('reason', '')}


def auto_choice():
    """What verifier="auto" means here: "claude" if the anthropic package is installed and ANTHROPIC_API_KEY
    (or ANTHROPIC_AUTH_TOKEN) is set, else None (images are downloaded for review, without early stopping)."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return None
    return 'claude' if os.environ.get('ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_AUTH_TOKEN') else None


def resolve(verifier, log=None):
    """'auto' -> see auto_choice(); 'claude' -> ClaudeVerifier(); a .csv path -> ReviewFileVerifier;
    None -> None; callable -> itself."""
    if verifier is None or callable(verifier):
        return verifier
    if verifier == 'auto':
        choice = auto_choice()
        if choice is None and log:
            log('verifier="auto": no Claude credentials (pip install "streetsight[claude]" and set '
                'ANTHROPIC_API_KEY), so images are downloaded for review without verification')
        return resolve(choice)
    if verifier == 'claude':
        return ClaudeVerifier()
    if str(verifier).lower().endswith('.csv'):
        return ReviewFileVerifier(verifier)
    raise ValueError(f'Unknown verifier {verifier!r}: use "auto", "claude", a review .csv, None, or a callable')
