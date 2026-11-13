"""基于完整 OpenAPI/Swagger 文档按实际调用端点在内存中过滤并管道生成 Pydantic v2 模型。

用法示例:
    # 使用内置官方 OpenAPI URL 直接更新 models/jellyfin.py
    uv run python -m scripts.generate_openapi_models jellyfin

    # 从本地 openapi.json 生成模型到指定路径
    uv run python -m scripts.generate_openapi_models jellyfin --input ./jellyfin-openapi.json --output models/jellyfin.py

    # 追加额外保留端点
    uv run python -m scripts.generate_openapi_models radarr --extra-path /api/v3/system/status

说明:
    默认直接输出到 ``models/<service>.py``（Webhook 回调模型已拆分至 ``models/*_webhook.py`` 手写维护）。
    需要环境中可用 ``datamodel-codegen``（可通过 ``uvx --from datamodel-code-generator datamodel-codegen`` 或 ``uv tool install datamodel-code-generator`` 提供）。
"""

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT_DIR = Path(__file__).resolve().parent.parent

# 各服务默认上游 OpenAPI 文档地址（用于本地一键同步与 GitHub Actions 定期更新）
DEFAULT_SPEC_URLS: dict[str, str] = {
    "radarr": "https://raw.githubusercontent.com/Radarr/Radarr/develop/src/Radarr.Api.V3/openapi.json",
    "sonarr": "https://raw.githubusercontent.com/Sonarr/Sonarr/main/src/Sonarr.Api.V3/openapi.json",
    "jellyfin": "https://api.jellyfin.org/openapi/jellyfin-openapi-stable.json",
    "emby": "https://raw.githubusercontent.com/MediaBrowser/Emby.SDK/refs/heads/master/Resources/OpenApi/openapi_v3.json",
}

# 各客户端实际调用的 API 端点模板（{param} 占位符会模糊匹配 OpenAPI 中的任意路径参数名）
SERVICE_ENDPOINTS: dict[str, list[tuple[str, str]]] = {
    "emby": [
        ("POST", "/Users/New"),
        ("GET", "/Users/{userId}"),
        ("DELETE", "/Users/{userId}"),
        ("POST", "/Users/{userId}/Policy"),
        ("POST", "/Users/{userId}/Password"),
        ("GET", "/Items"),
        ("POST", "/Items/{itemId}"),
        ("GET", "/Sessions"),
        ("GET", "/Library/VirtualFolders/Query"),
        ("GET", "/Library/SelectableMediaFolders"),
        ("GET", "/Devices/Info"),
        ("GET", "/System/Info/Public"),
    ],
    "jellyfin": [
        ("POST", "/Users/New"),
        ("GET", "/Users/{userId}"),
        ("DELETE", "/Users/{userId}"),
        ("POST", "/Users/{userId}/Policy"),
        ("POST", "/Users/Password"),
        ("GET", "/Items"),
        ("POST", "/Items/{itemId}"),
        ("GET", "/Sessions"),
        ("GET", "/Library/VirtualFolders"),
        ("GET", "/Devices/Info"),
        ("GET", "/System/Info/Public"),
    ],
    "sonarr": [
        ("GET", "/api/v3/series/lookup"),
        ("GET", "/api/v3/series"),
        ("POST", "/api/v3/series"),
        ("GET", "/api/v3/episode"),
        ("GET", "/api/v3/rootfolder"),
        ("GET", "/api/v3/qualityprofile"),
    ],
    "radarr": [
        ("GET", "/api/v3/movie/lookup/tmdb"),
        ("GET", "/api/v3/movie/lookup"),
        ("GET", "/api/v3/movie"),
        ("POST", "/api/v3/movie"),
        ("GET", "/api/v3/rootfolder"),
        ("GET", "/api/v3/qualityprofile"),
    ],
}

_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")


def _normalize_path_pattern(path: str) -> str:
    """将路径中的 ``{xxx}`` 参数名归一化，便于忽略参数命名差异进行匹配。"""
    cleaned = "/" + path.strip().lstrip("/")
    return _PATH_PARAM_RE.sub("{}", cleaned).lower()


def _load_spec(source: str) -> dict[str, Any]:
    """从本地文件或 HTTP(S) URL 读取 OpenAPI/Swagger JSON 文档。"""
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        req = Request(source, headers={"Accept": "application/json"})
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    path = Path(source).expanduser().resolve()
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_pointer(doc: dict[str, Any], pointer: str) -> Any:
    """按 JSON Pointer（如 ``#/components/schemas/UserDto``）从原文档读取目标对象。"""
    if not pointer.startswith("#/"):
        return None
    curr: Any = doc
    for raw_part in pointer[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        else:
            return None
    return curr


def _set_pointer(doc: dict[str, Any], pointer: str, value: Any) -> None:
    """按 JSON Pointer 将目标对象写入裁剪后的新文档中。"""
    if not pointer.startswith("#/"):
        return
    parts = [p.replace("~1", "/").replace("~0", "~") for p in pointer[2:].split("/")]
    curr = doc
    for part in parts[:-1]:
        if part not in curr or not isinstance(curr[part], dict):
            curr[part] = {}
        curr = curr[part]
    curr[parts[-1]] = copy.deepcopy(value)


def _collect_refs(node: Any, found: set[str]) -> None:
    """递归扫描对象树中的所有本地 ``$ref`` 引用路径。"""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            found.add(ref)
        for val in node.values():
            _collect_refs(val, found)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, found)


def _rewrite_refs(node: Any, ref_map: dict[str, str]) -> None:
    """递归替换文档树中已重命名的本地 ``$ref`` 引用路径。"""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref in ref_map:
            node["$ref"] = ref_map[ref]
        for val in node.values():
            _rewrite_refs(val, ref_map)
    elif isinstance(node, list):
        for item in node:
            _rewrite_refs(item, ref_map)


def _flatten_dotted_schema_names(filtered: dict[str, Any]) -> None:
    """将含命名空间点号的 Schema 名（如 ``A.B.UserDto``）扁平化，避免触发单文件输出限制。"""
    containers: list[tuple[str, dict[str, Any]]] = []
    schemas = (filtered.get("components") or {}).get("schemas")
    if isinstance(schemas, dict) and schemas:
        containers.append(("#/components/schemas/", schemas))
    definitions = filtered.get("definitions")
    if isinstance(definitions, dict) and definitions:
        containers.append(("#/definitions/", definitions))

    ref_map: dict[str, str] = {}
    for prefix, container in containers:
        if not any("." in name for name in container):
            continue

        # 统计短名称出现次数，无冲突时优先使用短名称（如 UserDto），冲突时保留完整下划线名
        short_counts: dict[str, int] = {}
        for name in container:
            short = name.rsplit(".", 1)[-1]
            short_counts[short] = short_counts.get(short, 0) + 1

        used_names: set[str] = {name for name in container if "." not in name}
        renamed: dict[str, Any] = {}
        for old_name, schema_val in container.items():
            if "." not in old_name:
                renamed[old_name] = schema_val
                continue
            short = old_name.rsplit(".", 1)[-1]
            candidate = (
                short if short_counts.get(short, 0) == 1 else old_name.replace(".", "_")
            )
            if candidate in used_names:
                candidate = old_name.replace(".", "_")
            while candidate in used_names:
                candidate += "_alt"
            used_names.add(candidate)
            renamed[candidate] = schema_val
            ref_map[f"{prefix}{old_name}"] = f"{prefix}{candidate}"

        container.clear()
        container.update(renamed)

    if ref_map:
        _rewrite_refs(filtered, ref_map)


def _collect_property_names(node: Any, prop_names: set[str]) -> None:
    """递归收集所有 Schema 中出现的 ``properties`` 字段名。"""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            prop_names.update(props.keys())
        for val in node.values():
            _collect_property_names(val, prop_names)
    elif isinstance(node, list):
        for item in node:
            _collect_property_names(item, prop_names)


def _strip_additional_properties_false(node: Any) -> None:
    """递归移除 ``additionalProperties: false``，避免生成 ``extra='forbid'`` 导致上游新增字段时反序列化失败。"""
    if isinstance(node, dict):
        if node.get("additionalProperties") is False:
            node.pop("additionalProperties", None)
        for val in node.values():
            _strip_additional_properties_false(val)
    elif isinstance(node, list):
        for item in node:
            _strip_additional_properties_false(item)


def _avoid_field_type_name_collisions(filtered: dict[str, Any]) -> None:
    """当 Schema 类名与字段名相同时重命名类型名，避免生成器给字段加 ``_1`` 后缀。"""
    containers: list[tuple[str, dict[str, Any]]] = []
    schemas = (filtered.get("components") or {}).get("schemas")
    if isinstance(schemas, dict) and schemas:
        containers.append(("#/components/schemas/", schemas))
    definitions = filtered.get("definitions")
    if isinstance(definitions, dict) and definitions:
        containers.append(("#/definitions/", definitions))

    prop_names: set[str] = set()
    _collect_property_names(filtered, prop_names)

    ref_map: dict[str, str] = {}
    for prefix, container in containers:
        used_names = set(container.keys())
        renamed: dict[str, Any] = {}
        for name, schema_val in container.items():
            if name not in prop_names:
                renamed[name] = schema_val
                continue
            suffix = (
                "Enum"
                if isinstance(schema_val, dict) and "enum" in schema_val
                else "Model"
            )
            candidate = f"{name}{suffix}"
            while candidate in used_names:
                candidate += "_"
            used_names.add(candidate)
            renamed[candidate] = schema_val
            ref_map[f"{prefix}{name}"] = f"{prefix}{candidate}"

        container.clear()
        container.update(renamed)

    if ref_map:
        _rewrite_refs(filtered, ref_map)


def filter_openapi_spec(
    spec: dict[str, Any],
    endpoints: list[tuple[str, str]],
) -> tuple[dict[str, Any], list[str]]:
    """按指定端点过滤 OpenAPI/Swagger 文档，并递归保留所有被引用的组件定义。

    Args:
        spec: 完整的 OpenAPI/Swagger 文档字典。
        endpoints: ``(HTTP方法, 路径)`` 列表；方法为空或 ``*`` 时匹配该路径下全部方法。

    Returns:
        ``(裁剪后的文档字典, 实际匹配到的路径与方法摘要列表)``。
    """
    filtered: dict[str, Any] = {}
    for meta_key in (
        "openapi",
        "swagger",
        "info",
        "servers",
        "host",
        "basePath",
        "schemes",
    ):
        if meta_key in spec:
            filtered[meta_key] = copy.deepcopy(spec[meta_key])
    filtered["paths"] = {}

    # 建立归一化路径 -> 期望 HTTP 方法集合的索引
    wanted_by_norm: dict[str, set[str]] = {}
    for method, raw_path in endpoints:
        norm = _normalize_path_pattern(raw_path)
        wanted_by_norm.setdefault(norm, set()).add(method.upper() if method else "*")

    matched_summary: list[str] = []
    raw_paths = spec.get("paths") or {}
    for spec_path, path_item in raw_paths.items():
        if not isinstance(path_item, dict):
            continue
        norm = _normalize_path_pattern(spec_path)
        if norm not in wanted_by_norm:
            continue

        wanted_methods = wanted_by_norm[norm]
        kept_item: dict[str, Any] = {}
        # 保留路径级别共享参数
        if "parameters" in path_item:
            kept_item["parameters"] = copy.deepcopy(path_item["parameters"])

        for key, op in path_item.items():
            if key == "parameters":
                continue
            if "*" in wanted_methods or key.upper() in wanted_methods:
                kept_item[key] = copy.deepcopy(op)
                matched_summary.append(f"{key.upper()} {spec_path}")

        if len(kept_item) > (1 if "parameters" in kept_item else 0):
            filtered["paths"][spec_path] = kept_item

    # 广度优先递归收集所有直接与间接 $ref 引用的 schema / parameter / response
    visited_refs: set[str] = set()
    queue: set[str] = set()
    _collect_refs(filtered["paths"], queue)

    while queue:
        ref = queue.pop()
        if ref in visited_refs:
            continue
        visited_refs.add(ref)
        target = _resolve_pointer(spec, ref)
        if target is None:
            continue
        _set_pointer(filtered, ref, target)
        _collect_refs(target, queue)

    _flatten_dotted_schema_names(filtered)
    _avoid_field_type_name_collisions(filtered)
    _strip_additional_properties_false(filtered)
    return filtered, matched_summary


def _find_codegen_command() -> list[str]:
    """定位可用的 ``datamodel-codegen`` 命令（优先本机二进制，其次 ``uvx``）。"""
    local_bin = shutil.which("datamodel-codegen")
    if local_bin:
        return [local_bin]
    uvx_bin = shutil.which("uvx")
    if uvx_bin:
        return [uvx_bin, "--from", "datamodel-code-generator", "datamodel-codegen"]
    raise RuntimeError(
        "未找到 datamodel-codegen 或 uvx，请先安装 datamodel-code-generator。"
    )


def run_codegen_from_stdin(
    filtered_spec: dict[str, Any],
    output_path: Path,
) -> None:
    """将内存中过滤后的 OpenAPI JSON 通过标准输入管道传给 ``datamodel-codegen``。"""
    cmd = _find_codegen_command() + [
        "--input-file-type",
        "openapi",
        "--openapi-scopes",
        "schemas",
        "paths",
        "parameters",
        "--output-model-type",
        "pydantic_v2.BaseModel",
        "--target-python-version",
        "3.11",
        "--use-union-operator",
        "--use-annotated",
        "--set-default-enum-member",
        "--collapse-root-models",
        "--use-standard-collections",
        "--disable-timestamp",
        "--output",
        str(output_path),
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(filtered_spec, ensure_ascii=False).encode("utf-8")
    subprocess.run(cmd, input=payload, check=True)


def main() -> None:
    """命令行入口：过滤 OpenAPI 文档并生成对应服务的 Pydantic v2 模型。"""
    parser = argparse.ArgumentParser(
        description="按实际调用端点过滤 OpenAPI/Swagger 并管道生成 Pydantic v2 模型"
    )
    parser.add_argument(
        "service",
        choices=sorted(SERVICE_ENDPOINTS.keys()),
        help="目标媒体服务名称",
    )
    parser.add_argument(
        "--input",
        default="",
        help="完整 openapi.json / swagger.json 的本地路径或 HTTP(S) URL（省略时使用内置官方 URL）",
    )
    parser.add_argument(
        "--output",
        default="",
        help="输出 Python 模型路径（默认 models/<service>.py）",
    )
    parser.add_argument(
        "--extra-path",
        action="append",
        default=[],
        help="额外保留的 API 路径（可重复传入，默认匹配该路径下全部 HTTP 方法）",
    )
    parser.add_argument(
        "--dump-filtered",
        default="",
        help="可选：将过滤后的 OpenAPI JSON 保存到指定路径以便排查",
    )
    args = parser.parse_args()

    endpoints = list(SERVICE_ENDPOINTS[args.service])
    for extra in args.extra_path:
        endpoints.append(("*", extra))

    spec_source = args.input or DEFAULT_SPEC_URLS[args.service]
    spec = _load_spec(spec_source)
    filtered_spec, matched = filter_openapi_spec(spec, endpoints)

    if not matched:
        print(
            "未在 OpenAPI 文档中匹配到任何预设端点，请检查文档版本或路径前缀。",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.dump_filtered:
        dump_path = Path(args.dump_filtered).expanduser().resolve()
        dump_path.write_text(
            json.dumps(filtered_spec, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"已导出过滤后的 OpenAPI 文档: {dump_path}")

    output_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else ROOT_DIR / "models" / f"{args.service}.py"
    )
    run_codegen_from_stdin(filtered_spec, output_path)

    schema_count = len(
        (filtered_spec.get("components") or {}).get("schemas") or {}
    ) + len(filtered_spec.get("definitions") or {})
    print(
        f"生成完成: {output_path}（匹配 {len(matched)} 个端点，保留 {schema_count} 个关联 Schema）"
    )


if __name__ == "__main__":
    main()
