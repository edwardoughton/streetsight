"""Decide whether a downloaded image shows the target.

A verifier is any callable  verifier(image_path, context) -> dict  with at least
  visible: bool | None   (None = could not decide)
  confidence: float 0-1
  reason: str
context holds target, distance_m, heading, pitch, fov, visible_above_m, target_height_m.
"""
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

PROMPT = """This is a Google Street View image. The camera was aimed, using map coordinates, at a {target}
about {distance_m:.0f} m away (compass heading {heading:.0f} deg, pitch {pitch:.0f} deg, horizontal field of view
{fov:.0f} deg). If the coordinates are right and nothing blocks the view, the {target} should appear near the
horizontal centre of the frame. Its registered height is about {target_height_m:.0f} m; buildings or trees may
hide its lower part.

Is the {target} itself clearly visible in this image? Count it as visible only if you can see the structure,
not merely a pole or wires that might belong to it. A clearly identifiable partial view (for example the upper
part above trees) counts as visible. Do not count look-alikes as the {target}: utility poles, streetlights,
flagpoles, electricity pylons, water towers or radio masts of another kind, unless that is what the {target} is.
Disguised towers (for example a "monopine" tree or a steeple) count if you can tell the structure is the target.

Reply with: visible (true/false), confidence (0 to 1), position of the target in the frame
(left, centre, right, or not_visible), and a one-sentence reason."""


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
                    {'type': 'text', 'text': PROMPT.format(**context)},
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


def resolve(verifier):
    """'claude' -> ClaudeVerifier(); a .csv path -> ReviewFileVerifier; None -> None; callable -> itself."""
    if verifier is None or callable(verifier):
        return verifier
    if verifier == 'claude':
        return ClaudeVerifier()
    if str(verifier).lower().endswith('.csv'):
        return ReviewFileVerifier(verifier)
    raise ValueError(f'Unknown verifier {verifier!r}: use "claude", a review .csv, None, or a callable')
