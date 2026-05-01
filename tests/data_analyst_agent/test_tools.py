import json
from unittest.mock import MagicMock

from utils.tools import make_athena_query_to_ci_csv


def _file_content_factory(path: str, text: str) -> dict:
    return {"path": path, "text": text}


def _write_files_action_factory(type: str, content: list[dict]) -> dict:
    return {"type": type, "content": content}


def _list_files_action_factory(type: str, path: str) -> dict:
    return {"type": type, "path": path}


def _build_tool(
    athena_client: MagicMock,
    code_interpreter_tool: MagicMock,
):
    session = MagicMock()
    session.client.return_value = athena_client
    return make_athena_query_to_ci_csv(
        session=session,
        region="us-east-1",
        code_interpreter_tool=code_interpreter_tool,
        ci_session_name="ci-test",
        tool_decorator=lambda fn: fn,
        file_content_factory=_file_content_factory,
        write_files_action_factory=_write_files_action_factory,
        list_files_action_factory=_list_files_action_factory,
    )


def test_athena_query_to_ci_csv_requires_database_when_no_default():
    athena = MagicMock()
    ci_tool = MagicMock()
    tool = _build_tool(athena, ci_tool)

    result = json.loads(tool(sql="SELECT 1 AS x", database=""))
    assert result["ok"] is False
    assert "database is required" in result["error"]


def test_athena_query_to_ci_csv_uses_explicit_database_arg():
    athena = MagicMock()
    athena.start_query_execution.return_value = {"QueryExecutionId": "qid-1"}
    athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
    athena.get_query_results.return_value = {
        "ResultSet": {
            "ResultSetMetadata": {"ColumnInfo": [{"Name": "x"}]},
            "Rows": [
                {"Data": [{"VarCharValue": "x"}]},
                {"Data": [{"VarCharValue": "1"}]},
            ],
        }
    }

    ci_tool = MagicMock()
    ci_tool.write_files.return_value = {"status": "success"}
    ci_tool.list_files.return_value = {"status": "success"}
    tool = _build_tool(athena, ci_tool)

    result = json.loads(tool(sql="SELECT 1 AS x", database="sample_database"))
    assert result["ok"] is True
    assert result["database"] == "sample_database"

    start_call_kwargs = athena.start_query_execution.call_args.kwargs
    assert start_call_kwargs["QueryExecutionContext"]["Database"] == "sample_database"
