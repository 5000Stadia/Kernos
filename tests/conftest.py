import os
import tempfile

# Set required environment variables before any kernos.* module is imported.
# load_dotenv() in app.py does not override existing env vars, so these take priority.
#
# CLEANUP-BATCH-V1 item 12 audit: the ANTHROPIC_API_KEY plant below is
# session-scoped on purpose. kernos modules check for the var at runtime
# inside service constructors and provider factories — many tests
# wouldn't pass without a value present. The companion subprocess-env
# scrub in kernos/kernel/external_agents/harnesses/claude_code.py is
# independent of this plant: it strips ANTHROPIC_API_KEY from the
# `claude --print` subprocess regardless of how the parent process got
# the value (developer .env, CI secret, or this plant). Both pieces are
# load-bearing — do not remove either without verifying the live-test
# sweep against KERNOS_LIVE_AGENT_TESTS=1.
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("OWNER_PHONE_NUMBER", "+15555550100")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+12345678901")

# Override KERNOS_INSTANCE_ID to empty during tests so adapters use fallback derivation.
# load_dotenv() won't override existing env vars, so setting it empty blocks .env leakage.
os.environ["KERNOS_INSTANCE_ID"] = ""

# Force Anthropic provider in tests so .env's KERNOS_LLM_PROVIDER doesn't bleed in.
os.environ["KERNOS_LLM_PROVIDER"] = "anthropic"

# Redirect KERNOS_DATA_DIR to a disposable tempdir for the whole test session.
# Without this, tests using ``_make_handler`` (which spins up the real
# FrictionObserver pointed at os.getenv("KERNOS_DATA_DIR", "./data"))
# write real friction reports into the repo's ./data/diagnostics/friction/
# — e.g. test_merged_messages_* tests produce MERGED_MESSAGES_DROPPED
# reports every run. Tests that explicitly monkeypatch KERNOS_DATA_DIR
# still override this.
os.environ.setdefault(
    "KERNOS_DATA_DIR", tempfile.mkdtemp(prefix="kernos-test-data-"),
)
