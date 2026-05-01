from example_python_shell.main import main


def test_main_runs_with_required_arg(capsys):
    rc = main(["--output_path", "s3://does-not-matter/"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "example_python_shell starting" in out
    assert "s3://does-not-matter/" in out
    assert "example_python_shell done" in out
