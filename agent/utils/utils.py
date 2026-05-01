import ast
import base64
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from strands_tools.code_interpreter import AgentCoreCodeInterpreter


def _json_default(o):
    try:
        return o.__dict__
    except Exception:
        return str(o)


def extract_text(response: Any) -> str:
    try:
        msg = getattr(response, "message", None)
        if isinstance(msg, dict):
            parts = msg.get("content", [])
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
            if texts:
                return "\n".join(texts)
    except Exception:
        pass
    return str(response)


def _extract_text_chunks(content_items: list[Any]) -> list[str]:
    chunks: list[str] = []
    for item in content_items:
        if not isinstance(item, dict):
            continue
        raw_text = item.get("text")
        if not isinstance(raw_text, str):
            continue

        # Code interpreter usually wraps payload as a Python-literal string.
        try:
            parsed = ast.literal_eval(raw_text)
        except (ValueError, SyntaxError):
            parsed = None

        if isinstance(parsed, list):
            for parsed_item in parsed:
                if isinstance(parsed_item, dict) and "text" in parsed_item:
                    chunks.append(str(parsed_item["text"]))
        else:
            chunks.append(raw_text)
    return chunks


def _parse_read_items(content_items: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in content_items:
        if isinstance(item, dict) and "resource" in item:
            items.append(item)
            continue
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            try:
                parsed = ast.literal_eval(item["text"])
            except (ValueError, SyntaxError):
                parsed = None
            if isinstance(parsed, list):
                items.extend(x for x in parsed if isinstance(x, dict))
    return items


def _decode_blob(blob: Any) -> bytes:
    if isinstance(blob, str):
        try:
            return base64.b64decode(blob, validate=True)
        except Exception:
            return blob.encode("utf-8")
    if isinstance(blob, (bytes, bytearray)):
        return bytes(blob)
    return bytes(blob)


def extract_artifacts_from_sandbox(
    code_interpreter_tool: "AgentCoreCodeInterpreter",
    ci_session_name: str,
    artifacts_dir: Path,
    sandbox_root_path: str = ".",
    max_files: int = 200,
) -> list[str]:
    """
    Extract files from a specific sandbox directory into local artifacts_dir.

    Args:
        code_interpreter_tool: The code interpreter tool instance
        ci_session_name: Session name for the code interpreter
        artifacts_dir: Local directory to save artifacts to
        sandbox_root_path: Path in sandbox to scan for artifacts
        max_files: Maximum number of files to extract

    Returns:
        A list of extracted artifact paths relative to artifacts_dir
    """
    logger = logging.getLogger(__name__)
    target_root = (sandbox_root_path or ".").strip() or "."
    logger.info(
        "Extracting artifacts from session '%s' under '%s' to %s",
        ci_session_name,
        target_root,
        artifacts_dir,
    )

    from strands_tools.code_interpreter.models import ExecuteCommandAction, ReadFilesAction

    try:
        # Discover files only in the target sandbox subtree.
        find_result = code_interpreter_tool.execute_command(
            ExecuteCommandAction(
                type="executeCommand",
                session_name=ci_session_name,
                command=f"find {target_root} -type f 2>/dev/null | sort",
            )
        )
        if find_result.get("status") != "success":
            print(f"Failed to list sandbox files: {find_result}")
            return []

        file_list_text = "\n".join(_extract_text_chunks(find_result.get("content", [])))
        if not file_list_text.strip():
            print(f"No files found in sandbox path: {target_root}")
            return []

        file_paths = sorted({line.strip() for line in file_list_text.splitlines() if line.strip()})
        if not file_paths:
            print(f"No files to extract from sandbox path: {target_root}")
            return []

        if len(file_paths) > max_files:
            print(
                f"Found {len(file_paths)} files in sandbox path '{target_root}'; "
                f"extracting first {max_files}."
            )
            file_paths = file_paths[:max_files]
        else:
            print(f"Found {len(file_paths)} files in sandbox path '{target_root}'.")

        read_result = code_interpreter_tool.read_files(
            ReadFilesAction(
                type="readFiles",
                session_name=ci_session_name,
                paths=file_paths,
            )
        )
        if read_result.get("status") != "success":
            print(f"Failed to read sandbox files: {read_result}")
            return []

        normalized_root = target_root.rstrip("/").lstrip("./")
        extracted_paths: list[str] = []
        for item in _parse_read_items(read_result.get("content", [])):
            resource = item.get("resource", {})
            uri = resource.get("uri", "")
            blob = resource.get("blob", "")
            if not uri:
                continue

            clean_path = uri.replace("file:///", "").lstrip("./")
            if normalized_root not in {"", "."} and not (
                clean_path == normalized_root or clean_path.startswith(normalized_root + "/")
            ):
                continue

            local_file_path = artifacts_dir / clean_path
            local_file_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                if blob:
                    local_file_path.write_bytes(_decode_blob(blob))
                    extracted_paths.append(clean_path)
                else:
                    print(f"No content for file: {uri}")
            except Exception as e:
                print(f"Error saving file {uri}: {e}")

        print(f"Successfully extracted {len(extracted_paths)} artifacts to {artifacts_dir}")
        return extracted_paths
    except Exception as e:
        print(f"Error extracting artifacts from sandbox: {e}")
        return []
