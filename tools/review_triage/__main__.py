"""``python -m tools.review_triage`` の入口。引数と終了コードは ``cli.main`` と ``README.rst`` を参照。"""

from tools.review_triage.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
