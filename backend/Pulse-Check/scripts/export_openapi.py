"""Write the OpenAPI spec to docs/openapi.json.

Run from the project root:

    python -m scripts.export_openapi
"""

import json
from pathlib import Path

from app.main import create_app

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"


def render_spec() -> str:
    return json.dumps(create_app().openapi(), indent=2) + "\n"


if __name__ == "__main__":
    SPEC_PATH.parent.mkdir(exist_ok=True)
    SPEC_PATH.write_text(render_spec())
    print(f"wrote {SPEC_PATH}")
