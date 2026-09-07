"""python -m app.validate outputs/issues.json — проверка контракта.

Задача P1-04 добавит поиск циклов.
"""

import json
import sys
from pathlib import Path

from app.models import IssuesFile


def main(path: str) -> int:
    f = IssuesFile.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
    ids = {i.id for i in f.issues}
    missing = [(i.id, d) for i in f.issues for d in i.depends_on if d not in ids]
    if missing:
        print("unknown depends_on:", missing)
        return 1
    print(f"ok: {len(f.issues)} issues, {len(f.phases)} phases")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
