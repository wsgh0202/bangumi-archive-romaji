#!/usr/bin/env python3
import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Set


ALIAS_BLOCK_RE = re.compile(r"(\|别名=\{)(.*?)(\})", re.DOTALL)


@dataclass
class MergeStats:
    anime_entries: int
    bangumi_entries: int = 0
    bangumi_ani_entries: int = 0
    matched_entries: int = 0
    identical_entries: int = 0
    merged_entries: int = 0
    written_entries: int = 0


def normalize(text: str) -> str:
    return text.strip().casefold()


def parse_infobox_aliases(infobox: str) -> List[str]:
    """从 infobox 的 `|别名={...}` 区块中提取别名列表。"""
    m = ALIAS_BLOCK_RE.search(infobox)
    if not m:
        return []

    body = m.group(2).strip("\r\n")
    aliases: List[str] = []
    for raw_line in re.split(r"\r?\n", body):
        line = raw_line.strip()
        if not line:
            continue

        # 别名格式是 [别名]，需要去掉方括号。
        if line.startswith("[") and line.endswith("]") and len(line) >= 2:
            candidate = line[1:-1].strip()
            if candidate:
                aliases.append(candidate)

    return aliases


def replace_infobox_aliases(infobox: str, aliases: List[str]) -> str:
    """替换 `|别名={...}` 内的别名。"""
    # 读取原 infobox 中的换行格式，保持一致性。
    newline = "\r\n" if "\r\n" in infobox else "\n"
    # 保持别名格式为 [别名]
    alias_lines = [f"[{a}]" for a in aliases]
    # 拼接成新的别名区块内容
    new_body = newline.join(alias_lines)

    # 若已存在别名区块，则替换其中内容，保持区块前后其他内容不变。
    m = ALIAS_BLOCK_RE.search(infobox)
    if m:
        replacement = f"{m.group(1)}{newline}{new_body}{newline}{m.group(3)}"
        return infobox[: m.start()] + replacement + infobox[m.end() :]

    return infobox


def replace_infobox_aliases_raw(raw_text: str, aliases: List[str]) -> str:
    """在 JSONL 原始行中，仅替换命中的 |别名={...} 区块。"""
    m = ALIAS_BLOCK_RE.search(raw_text)
    if not m:
        return raw_text

    newline = "\\r\\n" if "\\r\\n" in m.group(2) else "\\n"
    # 仅对新增的别名行做 JSON 字符串级转义，避免破坏原有其他内容。
    alias_lines = [json.dumps(f"[{a}]", ensure_ascii=False)[1:-1] for a in aliases]
    new_body = newline.join(alias_lines)
    replacement = f"{m.group(1)}{newline}{new_body}{newline}{m.group(3)}"
    return raw_text[: m.start()] + replacement + raw_text[m.end() :]


def render_stats_markdown(stats: MergeStats) -> str:
    """将合并统计渲染为 Markdown 文本。"""
    lines = [
        "### 合并统计",
        "",
        f"- anime-offline-database 总条目数：`{stats.anime_entries}`",
        f"- Bangumi Archive 总条目数：`{stats.bangumi_entries}`",
        f"- Bangumi Archive 动画条目数：`{stats.bangumi_ani_entries}`",
        f"- 匹配到的条目总数：`{stats.matched_entries}`",
        f"- 相同的条目数（匹配但无需更新）：`{stats.identical_entries}`",
        f"- 已合并的条目数：`{stats.merged_entries}`",
        f"- 合并文件条目总数：`{stats.written_entries}`",
    ]
    return "\n".join(lines) + "\n"


def write_markdown_report(path: Path, stats: MergeStats) -> None:
    """将合并统计写入 Markdown 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_stats_markdown(stats), encoding="utf-8")


def print_summary(output_path: Path, stats: MergeStats) -> None:
    """打印人类可读的合并统计摘要。"""
    print(f"完成：已写入到 {output_path}")
    print(f"anime-offline-database 总条目数：{stats.anime_entries}")
    print(f"Bangumi Archive 总条目数：{stats.bangumi_entries}")
    print(f"Bangumi Archive 动画条目数：{stats.bangumi_ani_entries}")
    print(f"匹配到的条目总数：{stats.matched_entries}")
    print(f"相同的条目数（匹配但无需更新）：{stats.identical_entries}")
    print(f"已合并的条目数：{stats.merged_entries}")
    print(f"合并文件条目总数：{stats.written_entries}")


def read_jsonl(path: Path) -> List[Dict]:
    rows: List[Dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[Dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            f.write("\n")


def build_anime_name_index(anime_rows: List[Dict]) -> Dict[str, List[str]]:
    """
    构建索引：规范化名称 -> 对应条目的全部名称（title + synonyms）。
    """
    index: Dict[str, List[str]] = {}

    for row in anime_rows:
        names: List[str] = []
        # 从 title 字段收集名称
        title = row.get("title")
        if isinstance(title, str) and title.strip():
            names.append(title.strip())

        # 从 synonyms 字段收集所有名称
        synonyms = row.get("synonyms")
        if isinstance(synonyms, list):
            for s in synonyms:
                if isinstance(s, str) and s.strip():
                    names.append(s.strip())

        # 先在单条 anime 记录内部去重，避免同义名重复污染索引。
        unique_names: List[str] = []
        seen_local: Set[str] = set()
        for n in names:
            k = normalize(n)
            if k and k not in seen_local:
                seen_local.add(k)
                unique_names.append(n)

        if not unique_names:
            continue

        def append_unique(target: List[str], values: List[str]) -> None:
            # 在保留原有顺序的前提下，追加不重复名称。
            seen = {normalize(n) for n in target}
            for n in values:
                key = normalize(n)
                if key not in seen:
                    target.append(n)
                    seen.add(key)

        # 对于该条记录中的每个名字，都指向同一组去重后的名字列表，
        # 这样后续按任意别名命中时，都能拿到完整候选集合。
        for n in unique_names:
            key = normalize(n)
            if key not in index:
                index[key] = []
            append_unique(index[key], unique_names)

    return index


def merge_aliases_stream(
    anime_rows: List[Dict], bangumi_path: Path, output_path: Path
) -> MergeStats:
    """逐条读取 Bangumi JSONL，并将合并结果逐条写入输出文件。

    Returns:
        MergeStats: 合并统计结果
    """
    anime_index = build_anime_name_index(anime_rows)
    stats = MergeStats(anime_entries=len(anime_rows))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with (
        bangumi_path.open("r", encoding="utf-8") as src,
        output_path.open("w", encoding="utf-8") as dst,
    ):
        for line in src:
            line = line.strip()
            if not line:
                continue

            stats.bangumi_entries += 1

            subject = json.loads(line)

            # 仅处理 type=2（动画）条目
            subject_type = (
                subject.get("type") if isinstance(subject.get("type"), int) else 0
            )
            if subject_type != 2:
                dst.write(line)
                dst.write("\n")
                stats.written_entries += 1
                continue

            stats.bangumi_ani_entries += 1

            # 读取名称相关字段
            name = subject.get("name") if isinstance(subject.get("name"), str) else ""
            name_cn = (
                subject.get("name_cn")
                if isinstance(subject.get("name_cn"), str)
                else ""
            )
            infobox = (
                subject.get("infobox")
                if isinstance(subject.get("infobox"), str)
                else ""
            )
            existing_aliases = parse_infobox_aliases(infobox)

            # 收集已存在名字集合（name/name_cn/现有别名），用于避免回填重复内容。
            existing_norm: Set[str] = set()
            for v in [name, name_cn, *existing_aliases]:
                if v.strip():
                    existing_norm.add(normalize(v))

            # 只要 Bangumi 的 name/name_cn/别名中任一项命中 anime 索引，即纳入候选。
            matched_name_sets: List[str] = []
            for key in existing_norm:
                if key in anime_index:
                    matched_name_sets = anime_index[key]
                    break

            if matched_name_sets:
                stats.matched_entries += 1

            # 保持别名顺序稳定：先保留原有别名，再追加新合并名称。
            merged_aliases: List[str] = list(existing_aliases)
            merged_norm_aliases: Set[str] = {normalize(a) for a in existing_aliases}

            # 追加“不在原字段里”的名称。
            for candidate in matched_name_sets:
                ckey = normalize(candidate)
                if not ckey:
                    continue
                if ckey in existing_norm or ckey in merged_norm_aliases:
                    continue
                merged_aliases.append(candidate)
                merged_norm_aliases.add(ckey)

            # 如果别名没有变化，直接写回原行，保证完全不改动原始内容。
            if merged_aliases == existing_aliases:
                dst.write(line)
                dst.write("\n")
                stats.identical_entries += 1 if matched_name_sets else 0
                stats.written_entries += 1
                continue

            # 每行最多一个 |别名 区块，直接替换该命中片段即可，其他内容保持原样。
            new_line = replace_infobox_aliases_raw(line, merged_aliases)

            dst.write(new_line)
            dst.write("\n")
            stats.merged_entries += 1 if matched_name_sets else 0
            stats.written_entries += 1

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "当 Bangumi 的 name/name_cn/别名中存在大小写不敏感的完全匹配时，"
            "将 anime-offline JSONL 里的 title/synonyms 合并到 Bangumi infobox 别名。"
        )
    )
    parser.add_argument(
        "--anime",
        default="build/anime-offline-database.jsonl",
        help="anime-offline JSONL 路径",
    )
    parser.add_argument(
        "--bangumi",
        default="build/bangumi_archive/subject.jsonlines",
        help="Bangumi JSONL 路径",
    )
    parser.add_argument(
        "--output", default="build/bangumi_alias_merged.jsonl", help="输出 JSONL 路径"
    )
    parser.add_argument(
        "--report-markdown",
        help="可选：将合并统计写入指定 Markdown 路径。",
    )
    args = parser.parse_args()

    anime_path = Path(args.anime)
    bangumi_path = Path(args.bangumi)
    output_path = Path(args.output)

    anime_rows = read_jsonl(anime_path)
    stats = merge_aliases_stream(anime_rows, bangumi_path, output_path)

    print_summary(output_path, stats)

    if args.report_markdown:
        report_path = Path(args.report_markdown)
        write_markdown_report(report_path, stats)
        print(f"已生成合并统计报告：{report_path}")


if __name__ == "__main__":
    main()
