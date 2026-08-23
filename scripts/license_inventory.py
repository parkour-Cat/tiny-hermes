"""Generate the third-party licence inventory §28 asks for before release.

Generated rather than written by hand, because a hand-written inventory is
correct on the day it is written and wrong on the next `uv sync`. Run it and
commit the output; a diff in `THIRD_PARTY_LICENSES.md` is then a review
prompt rather than something nobody notices.

This reads what is **installed**, which is what actually ships, not what
`pyproject.toml` asks for. The two differ by every transitive dependency,
and it is the transitive ones that carry the surprises.

Both sides of the repository, because a list covering only Python would
have been read as covering the product. The frontend half comes from
`pnpm licenses list --prod`, which resolves the same lockfile pnpm installs
from — asking pnpm beats walking `node_modules` by hand, which is a
different question with a similar-looking answer.

Not legal advice — product design §28's own last line says the licence
conclusions need review by someone qualified before release, and this file
does not change that.

Usage::

    uv run --no-sync python scripts/license_inventory.py > THIRD_PARTY_LICENSES.md
"""

import importlib.metadata as metadata
import json
import subprocess  # noqa: S404 - asking pnpm is the point
from collections import Counter
from typing import Any

#: Licences that carry obligations beyond attribution. Nothing here blocks a
#: release; they are surfaced because "all permissive" is a claim somebody
#: will make about this list, and it should be made knowingly.
NOTABLE = {"MPL-2.0", "MPL 2.0", "LGPL", "GPL", "AGPL", "EPL-1.0", "EPL-2.0", "CDDL"}


def _licence(dist: metadata.Distribution) -> str:
    """The clearest licence string a distribution offers.

    Three sources in order of trustworthiness: the modern SPDX expression,
    the legacy free-text field, and finally the classifiers. The free-text
    field is skipped when it is long, because some packages paste their
    entire licence into it and a table is not the place for that.
    """
    expression = dist.metadata.get("License-Expression")
    if expression:
        return str(expression).strip()
    legacy = dist.metadata.get("License")
    if legacy and len(str(legacy)) <= 60:
        return str(legacy).strip()
    classifiers = [
        line
        for line in (dist.metadata.get_all("Classifier") or [])
        if str(line).startswith("License ::")
    ]
    if classifiers:
        return str(classifiers[0]).split("::")[-1].strip()
    return "UNKNOWN"


def _frontend_rows() -> list[tuple[str, str, str]] | None:
    """`None` when pnpm cannot answer — a missing install, or no pnpm.

    Distinguished from "no dependencies" on purpose: a silent empty section
    would read as "the frontend has none", which is the wrong conclusion to
    hand somebody reviewing licences before a release.
    """
    try:
        answer = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["pnpm", "licenses", "list", "--prod", "--json"],  # noqa: S607 - on PATH by design
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if answer.returncode != 0 or not answer.stdout.strip():
        return None
    try:
        listed: dict[str, list[dict[str, Any]]] = json.loads(answer.stdout)
    except json.JSONDecodeError:
        return None
    rows: list[tuple[str, str, str]] = []
    for licence, packages in listed.items():
        for package in packages:
            name = str(package.get("name", "?"))
            for version in package.get("versions", ["?"]):
                rows.append((name, str(version), licence))
    return sorted(rows, key=lambda row: row[0].lower())


def main() -> int:
    rows: list[tuple[str, str, str]] = []
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        # This project is not one of its own third-party dependencies, and
        # leaving it in put it under "licence unreadable" — which is where a
        # reader would go looking for a real problem.
        if str(name) == "tiny-hermes":
            continue
        rows.append((str(name), dist.version, _licence(dist)))
    rows.sort(key=lambda row: row[0].lower())

    counts = Counter(row[2] for row in rows)
    notable = sorted({row[0] for row in rows if row[2] in NOTABLE})
    unknown = sorted({row[0] for row in rows if row[2] == "UNKNOWN"})

    print("# 第三方许可证清单")
    print()
    print("> 由 `scripts/license_inventory.py` 生成，读的是**已安装**的依赖——")
    print("> 也就是真正会随发布走的那些，而不是 `pyproject.toml` 声明的那些。")
    print("> 两者相差的正是传递依赖，而意外通常出在传递依赖上。")
    print(">")
    print("> **本文件不构成法律意见**（产品设计 §28 最后一句）。")
    print()
    # Counted separately and said so. One total across both would hide which
    # side a number came from, and the two are installed by different tools
    # from different lockfiles.
    print(f"本项目自身采用 Apache-2.0。Python 依赖 {len(rows)} 个；前端见文末。")
    print()
    print("## Python 依赖的分布")
    print()
    print("| 许可证 | 数量 |")
    print("|---|---:|")
    for licence, count in counts.most_common():
        print(f"| {licence} | {count} |")
    print()
    if notable:
        print("## 需要留意的")
        print()
        print("以下依赖的许可证带有超出「署名」的义务。**它们都不阻塞发布**，")
        print("列在这里是因为「全都是宽松许可」这句话会有人说出口，而它应该被知情地说。")
        print()
        for name in notable:
            print(f"- `{name}`")
        print()
    if unknown:
        print("## 元数据里读不出许可证的")
        print()
        print("**这些需要人工确认**，不能因为脚本读不出来就当作没问题。")
        print()
        for name in unknown:
            print(f"- `{name}`")
        print()
    print("## 全部 Python 依赖")
    print()
    print("| 包 | 版本 | 许可证 |")
    print("|---|---|---|")
    for name, version, licence in rows:
        print(f"| {name} | {version} | {licence} |")

    print()
    print("## 前端（pnpm，生产依赖）")
    print()
    frontend = _frontend_rows()
    if frontend is None:
        print("**没有读到。** `pnpm licenses list --prod --json` 没有给出答案——")
        print("可能是没装依赖，也可能是环境里没有 pnpm。**这不等于「前端没有依赖」**，")
        print("发布前必须重新生成一次并确认这一节有内容。")
        return 0
    frontend_counts = Counter(row[2] for row in frontend)
    frontend_notable = sorted({row[0] for row in frontend if row[2] in NOTABLE})
    print(f"共 {len(frontend)} 个。")
    print()
    print("| 许可证 | 数量 |")
    print("|---|---:|")
    for licence, count in frontend_counts.most_common():
        print(f"| {licence} | {count} |")
    print()
    if frontend_notable:
        print("### 需要留意的")
        print()
        for name in frontend_notable:
            print(f"- `{name}`")
        print()
    print("| 包 | 版本 | 许可证 |")
    print("|---|---|---|")
    for name, version, licence in frontend:
        print(f"| {name} | {version} | {licence} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
