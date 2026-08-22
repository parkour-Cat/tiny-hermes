"""Generate the third-party licence inventory §28 asks for before release.

Generated rather than written by hand, because a hand-written inventory is
correct on the day it is written and wrong on the next `uv sync`. Run it and
commit the output; a diff in `THIRD_PARTY_LICENSES.md` is then a review
prompt rather than something nobody notices.

This reads what is **installed**, which is what actually ships, not what
`pyproject.toml` asks for. The two differ by every transitive dependency,
and it is the transitive ones that carry the surprises.

Not legal advice — product design §28's own last line says the licence
conclusions need review by someone qualified before release, and this file
does not change that.

Usage::

    uv run --no-sync python scripts/license_inventory.py > THIRD_PARTY_LICENSES.md
"""

import importlib.metadata as metadata
from collections import Counter

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
    print(f"本项目自身采用 Apache-2.0。依赖共 {len(rows)} 个。")
    print()
    print("## 分布")
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
    print("## 全部依赖")
    print()
    print("| 包 | 版本 | 许可证 |")
    print("|---|---|---|")
    for name, version, licence in rows:
        print(f"| {name} | {version} | {licence} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
