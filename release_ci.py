#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from datetime import datetime, timezone


DEFAULT_BANGUMI_VERSION_FILE = Path("version/bangumi.json")
DEFAULT_ANIME_VERSION_FILE = Path("version/anime-offline-database.json")
DEFAULT_BANGUMI_BUILD_DIR = Path("build/bangumi_archive")
DEFAULT_MERGED_SUBJECT_FILE = Path("build/bangumi_alias_merged.jsonl")
DEFAULT_MERGED_OUTPUT_NAME = Path("bangumi-archive-merged.zip")
DEFAULT_DIST_DIR = Path("dist")
DEFAULT_LATEST_JSON = Path("version/latest.json")


def read_markdown_sections(paths: list[str] | None) -> list[str]:
    """读取额外的 Markdown 片段，并过滤掉空内容。"""
    sections: list[str] = []
    for raw_path in paths or []:
        content = Path(raw_path).read_text(encoding="utf-8").strip()
        if content:
            sections.append(content)
    return sections


def load_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 编码的 JSON 文件并返回解析结果。"""
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    """保存最新版本信息 JSON 数据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def sha256_file(path: Path) -> str:
    """计算文件的 SHA-256 摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_archive_name(version_file: Path) -> str:
    """根据版本元数据确定发布压缩包文件名。"""
    version = load_json(version_file)
    name = str(version.get("name", "")).strip()
    if name:
        return name
    return "bangumi-archive-romaji.zip"


def build_release_asset_url(
    asset_name: str,
    browser_download_url: str | None,
    github_repo: str | None,
    release_tag: str | None,
) -> str:
    """返回显式指定的资源地址，或根据 GitHub Release 上下文推导下载地址。"""
    if browser_download_url:
        return browser_download_url

    repo = github_repo or os.environ.get("GITHUB_REPOSITORY", "").strip()
    tag = release_tag or os.environ.get("GITHUB_REF_NAME", "").strip()
    if not repo or not tag:
        raise ValueError(
            "必须提供 --browser-download-url，或同时提供 --github-repo 与 --release-tag。"
        )
    return f"https://github.com/{repo}/releases/download/{tag}/{asset_name}"


def package_zip(args: argparse.Namespace) -> int:
    """将 Bangumi 构建产物打包为可发布的 ZIP 文件。"""
    source_dir = Path(args.source_dir)
    merged_subject = Path(args.merged_subject)
    output_dir = Path(args.output_dir)

    # 在生成发布包之前，先校验输入路径是否存在。
    if not source_dir.is_dir():
        print(f"错误：目录不存在 {source_dir}", file=sys.stderr)
        return 1
    if not merged_subject.is_file():
        print(f"错误：文件不存在 {merged_subject}", file=sys.stderr)
        return 1

    output_name = args.output_name
    output_path = output_dir / output_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # 拷贝所有生成的数据文件，但将原始 subject 文件替换为供发布使用的别名合并版本。
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(source_dir.iterdir()):
            if not file_path.is_file() or file_path.name == "subject.jsonlines":
                continue
            archive.write(file_path, arcname=file_path.name)

        archive.write(merged_subject, arcname="subject.jsonlines")

    print(output_path)
    return 0


def write_latest_json(args: argparse.Namespace) -> int:
    """为已打包的发布资源生成 version/latest.json。"""
    asset_name = args.asset_name
    asset_path = (
        Path(args.asset_path) if args.asset_path else Path(args.output_dir) / asset_name
    )
    output_path = Path(args.output)

    # 在生成元数据前，确认打包产物确实存在。
    if not asset_path.is_file():
        print(f"错误：打包文件不存在 {asset_path}", file=sys.stderr)
        return 1

    # 优先使用显式传入的下载地址，
    # 否则根据命令行参数或 CI 环境变量推导标准的 GitHub Release 资源地址。
    try:
        browser_download_url = build_release_asset_url(
            asset_name=asset_name,
            browser_download_url=args.browser_download_url,
            github_repo=args.github_repo,
            release_tag=args.release_tag,
        )
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    # 获取文件时间
    created_at = (
        datetime.fromtimestamp(asset_path.stat().st_mtime, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    payload = {
        "browser_download_url": browser_download_url,
        "digest": f"sha256:{sha256_file(asset_path)}",
        "name": asset_name,
        "created_at": created_at,
    }
    save_json(output_path, payload)
    print(output_path)
    return 0


def commit_version(args: argparse.Namespace) -> int:
    """在版本元数据发生变化时提交 version 目录。"""
    repo_root = Path(args.repo_root)
    version_dir = Path(args.version_dir)

    # 如果版本元数据没有变化，则跳过空提交。
    status_cmd = [
        "git",
        "-C",
        str(repo_root),
        "status",
        "--porcelain",
        "--",
        str(version_dir),
    ]
    status = subprocess.run(status_cmd, check=True, capture_output=True, text=True)
    if not status.stdout.strip():
        print("version 目录没有变更，无需提交。")
        return 0

    # 仅暂存 version 目录，并创建专门的元数据提交。
    subprocess.run(
        ["git", "-C", str(repo_root), "add", "--", str(version_dir)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-m", args.message],
        check=True,
    )
    print(args.message)
    return 0


def render_release_notes(args: argparse.Namespace) -> int:
    """根据记录的 Bangumi 与 anime 源数据生成发布说明。"""
    bangumi = load_json(Path(args.bangumi_version_file))
    anime = load_json(Path(args.anime_version_file))
    extra_sections = read_markdown_sections(args.append_markdown)

    # 统一整理源数据字段，缺失值回退为可读的默认内容。
    bangumi_name = str(bangumi.get("name") or "未知")
    bangumi_url = str(
        bangumi.get("browser_download_url") or bangumi.get("url") or ""
    ).strip()
    bangumi_digest = str(bangumi.get("digest") or "").strip()

    anime_tag = str(
        anime.get("tag") or anime.get("release") or anime.get("asset") or "未知"
    )
    anime_url = str(anime.get("source") or "").strip()
    anime_asset = str(anime.get("asset") or "").strip()
    anime_sha256 = str(anime.get("sha256") or "").strip()

    # 拼接发布说明
    lines = [
        "### 数据版本",
        "",
        "- [Bangumi Archive](https://github.com/bangumi/Archive)",
        f"  - 版本：`{bangumi_name}`",
        f"  - 地址：<{bangumi_url or '无'}>",
        f"  - 摘要：`{bangumi_digest or '无'}`",
        "- [anime-offline-database](https://github.com/manami-project/anime-offline-database)",
        f"  - 版本：`{anime_tag}`",
        f"  - 资源：`{anime_asset or '无'}`",
        f"  - 地址：<{anime_url or '无'}>",
        f"  - sha256：`{anime_sha256 or '无'}`",
    ]

    if extra_sections:
        lines.extend(["", *extra_sections])

    content = "\n".join(lines) + "\n"

    # 在 CI 中写入文件，本地调试时也支持直接输出到标准输出。
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        print(output_path)
    else:
        sys.stdout.write(content)

    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建 CI 辅助脚本的命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="CI helper for packaging merged Bangumi artifacts and release metadata.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 配置用于生成发布 ZIP 的子命令。
    package_parser = subparsers.add_parser(
        "package-zip",
        help="打包 build/bangumi_archive 与合并后的 subject.jsonlines。",
    )
    package_parser.add_argument(
        "--source-dir",
        default=str(DEFAULT_BANGUMI_BUILD_DIR),
        help="Bangumi build 目录。",
    )
    package_parser.add_argument(
        "--merged-subject",
        default=str(DEFAULT_MERGED_SUBJECT_FILE),
        help="合并后的 subject 文件路径。",
    )
    package_parser.add_argument(
        "--output-dir", default=str(DEFAULT_DIST_DIR), help="zip 输出目录。"
    )
    package_parser.add_argument(
        "--output-name",
        default=str(DEFAULT_MERGED_OUTPUT_NAME),
        help="zip 文件名。",
    )
    package_parser.set_defaults(func=package_zip)

    # 配置用于生成 latest.json 元数据的子命令。
    latest_parser = subparsers.add_parser(
        "write-latest-json",
        help="为已打包 zip 生成 version/latest.json。",
    )
    latest_parser.add_argument("--asset-path", help="已打包 zip 的路径。")
    latest_parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_DIST_DIR),
        help="当未显式提供 --asset-path 时，用于推导 zip 所在目录。",
    )
    latest_parser.add_argument(
        "--asset-name", default=str(DEFAULT_MERGED_OUTPUT_NAME), help="发布资源名。"
    )
    latest_parser.add_argument(
        "--browser-download-url",
        help="完整下载链接。若未提供，则使用 --github-repo 与 --release-tag 组合生成。",
    )
    latest_parser.add_argument(
        "--github-repo", help="GitHub 仓库，例如 wsgh0202/bangumi-archive-romaji。"
    )
    latest_parser.add_argument("--release-tag", help="GitHub Release tag。")
    latest_parser.add_argument(
        "--output", default=str(DEFAULT_LATEST_JSON), help="latest.json 输出路径。"
    )
    latest_parser.set_defaults(func=write_latest_json)

    # 配置用于提交版本元数据变更的子命令。
    commit_parser = subparsers.add_parser(
        "commit-version",
        help="提交 version 目录变更。",
    )
    commit_parser.add_argument("--repo-root", default=".", help="git 仓库根目录。")
    commit_parser.add_argument(
        "--version-dir", default="version", help="需要提交的版本目录。"
    )
    commit_parser.add_argument(
        "--message",
        default="chore: update version metadata",
        help="git commit message。",
    )
    commit_parser.set_defaults(func=commit_version)

    # 配置用于生成 Release 页面说明文本的子命令。
    notes_parser = subparsers.add_parser(
        "render-release-notes",
        help="根据 version 元数据生成 Release 页面说明文本。",
    )
    notes_parser.add_argument(
        "--bangumi-version-file",
        default=str(DEFAULT_BANGUMI_VERSION_FILE),
        help="Bangumi 版本元数据文件。",
    )
    notes_parser.add_argument(
        "--anime-version-file",
        default=str(DEFAULT_ANIME_VERSION_FILE),
        help="anime-offline-database 版本元数据文件。",
    )
    notes_parser.add_argument(
        "--append-markdown",
        action="append",
        help="追加写入 release notes 的 Markdown 文件，可重复指定。",
    )
    notes_parser.add_argument(
        "--output", help="说明文本输出路径；未提供时输出到 stdout。"
    )
    notes_parser.set_defaults(func=render_release_notes)

    return parser


def main() -> int:
    """解析命令行参数并分发到选中的子命令。"""
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
