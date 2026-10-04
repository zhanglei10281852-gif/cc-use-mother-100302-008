"""机器人产业基金里程碑拨款命令行冒烟入口。"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from industry_fund import InvestmentCase


def main() -> None:
    item = InvestmentCase(**{'case_code': 'case-code-001', 'applicant': 'applicant-001', 'round_name': 'round-name-001', 'state': 'draft'})
    print(json.dumps({"item": asdict(item), "fingerprint": item.fingerprint()}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
