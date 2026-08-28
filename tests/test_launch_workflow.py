"""launch.yml contract tests — the workflow's dispatch semantics are
operational surface, so they're pinned like code."""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "launch.yml"


def yaml_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_auto_reset_dead_input_defaults_true():
    text = yaml_text()
    assert "auto_reset_dead:" in text
    # boolean input, default true (both lines adjacent in the inputs block)
    assert 'description: "Revive all DEAD_FETCH products before wave 1"' in text
    reset_block = text.split("auto_reset_dead:")[1][:200]
    assert "type: boolean" in reset_block
    assert "default: true" in reset_block


def test_reset_step_runs_all_before_waves():
    text = yaml_text()
    reset_step = text.index("Auto-reset dead catalog")
    waves_step = text.index("Execute launch waves")
    assert reset_step < waves_step, "reset must precede wave execution"
    reset_section = text[reset_step:waves_step]
    assert "reset-products --all" in reset_section
    # condition honors both boolean and string dispatch representations
    assert "inputs.auto_reset_dead == true" in reset_section
    assert "inputs.auto_reset_dead == 'true'" in reset_section


def test_workflow_env_carries_validated_secrets_for_reset_and_waves():
    text = yaml_text()
    # env hoisted to workflow level: shared by reset + waves, single source
    env_block = text.split("env:")[1].split("concurrency:")[0]
    for secret in (
        "MONGO_URI", "MONGO_DB", "TOKEN_MASTER_KEY",          # reset needs these
        "GEMINI_API_KEY", "ALIEXPRESS_APP_KEY",               # waves need these
        "PINTEREST_APP_ID", "TELEGRAM_BOT_TOKEN",
    ):
        assert secret in env_block, f"{secret} missing from workflow env"
    assert "secrets.BRIDGE_PAT || secrets.GITHUB_BRIDGE_PAT" in env_block
    # per-step env duplication is gone (exactly one secrets mapping)
    assert text.count("secrets.MONGO_URI") == 1


def test_boost_guard_and_wave_semantics_unchanged():
    text = yaml_text()
    assert "waves>2 requires boost=true" in text
    assert "--daily-cap-override 4" in text
    assert "sleep 1500" in text                     # 25-min board spacing
    assert "timeout-minutes: 145" in text
    assert "group: pin-runner" in text              # never races the cron
