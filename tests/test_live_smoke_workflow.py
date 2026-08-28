"""live_smoke.yml contract tests — quoting incident regression (exit code 2:
multi-word `--keywords kitchen organizer` split into positionals)."""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "live_smoke.yml"


def yaml_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_inputs_are_double_quoted_in_arg_array():
    """THE regression: unquoted ${{ inputs.keywords }} word-split multi-word
    phrases into stray positionals (exit code 2). Every optional input must
    expand inside double quotes as its own array element."""
    text = yaml_text()
    for input_name in ("account", "keywords", "board_id"):
        flag = f"--{input_name.replace('_', '-')}"
        expected = f'ARGS+=({flag} "${{{{ inputs.{input_name} }}}}")'
        assert expected in text, f"{flag} must be double-quoted"


def test_no_eval_and_no_unquoted_expansions():
    """eval + string-concatenated inputs is injection-adjacent and was the
    original word-splitting vector. The array idiom replaces both."""
    text = yaml_text()
    assert "\neval " not in text
    assert "ARGS=\"$ARGS" not in text            # old string-concat pattern gone
    assert 'ARGS+=(--keywords "${{' in text      # array element form present


def test_skip_pin_conditional_and_defaults():
    text = yaml_text()
    assert 'if [ "${{ inputs.skip_pin }}" = "true" ]; then' in text
    assert "ARGS+=(--skip-pin)" in text
    assert 'default: "kitchen organizer"' in text  # multi-word default retained


def test_live_apis_and_secrets_wired():
    text = yaml_text()
    assert "python scripts/live_smoke_test.py" in text
    env_block = text.split("env:")[1].split("steps:")[0]
    for secret in ("MONGO_URI", "TOKEN_MASTER_KEY", "ALIEXPRESS_APP_KEY",
                   "ALIEXPRESS_APP_SECRET", "ALIEXPRESS_TRACKING_ID",
                   "BRIDGE_PAT", "PINTEREST_APP_ID", "PINTEREST_APP_SECRET"):
        assert secret in env_block, f"{secret} missing"
    assert "concurrency:\n  group: live-smoke" in text
    assert "timeout-minutes: 15" in text


def test_push_trigger_and_atlas_persistence():
    """Autonomous iteration contract: push to main triggers the live smoke,
    and the script persists stage evidence to Atlas (smoke_reports) so the
    engineer can read results without Actions log access."""
    text = yaml_text()
    assert "branches: [main]" in text and "workflow_dispatch:" in text
    script = (WORKFLOW.parent.parent.parent / "scripts" / "live_smoke_test.py").read_text(
        encoding="utf-8")
    assert "smoke_reports" in script
    assert "class StageReport" in script
    assert "report.finish(ok=True, pin_url=" in script
