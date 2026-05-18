"""D3.0-fu1 — silver/ dbt project structural contracts (2026-05-18).

Plan doc: ``kb/decisions/d3-0-silver-foundations-plan.md`` (local-only).

D3.0 shipped direct-DuckDB ETL with the dbt project layering deferred to
D3.0-fu1. This file pins the dbt project's structural shape: dbt_project.yml,
profiles.yml.example, 5 model files, and the shared envelope/bronze macros.
Behavioral correctness (silver Parquet output content + idempotency) is
already pinned by ``tests/integration/test_silver_etl_pipeline.py``; these
contracts are the **structural pre-conditions** that the behavioral tests
implicitly depend on — if any of these files goes missing, the integration
tests would error in non-obvious ways before the behavioral assertion fires.
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SILVER_DIR = REPO_ROOT / "silver"


# ─── 1. Top-level dbt project files ─────────────────────────────────────────


def test_dbt_project_yml_exists():
    """``silver/dbt_project.yml`` exists at the silver/ root.

    Required for ``dbt run --project-dir silver/`` to recognize silver as
    a dbt project. The wrapper at ``silver/scripts/etl_run.py`` passes
    this path via ``--project-dir`` to the dbt subprocess.
    """
    p = SILVER_DIR / "dbt_project.yml"
    assert p.is_file(), (
        f"{p} missing. D3.0-fu1 (2026-05-18) layers the dbt project on "
        "top of the direct-DuckDB ETL; dbt_project.yml at silver/ root is "
        "the project marker dbt looks for."
    )


def test_dbt_project_yml_declares_silver_profile():
    """dbt_project.yml binds the project to the ``silver`` profile name.

    The wrapper writes a tmp profiles.yml that declares a top-level
    ``silver:`` entry; mismatching the profile name here would cause
    `dbt run` to fail with "Could not find profile named 'silver'".
    """
    import yaml
    p = SILVER_DIR / "dbt_project.yml"
    if not p.is_file():
        pytest.skip("dbt_project.yml missing — covered by sibling test")
    data = yaml.safe_load(p.read_text())
    assert data.get("profile") == "silver", (
        f"dbt_project.yml profile must be 'silver'; got {data.get('profile')!r}. "
        "The wrapper's tmp profiles.yml uses the top-level key 'silver:' — "
        "the two names MUST agree or dbt errors before any model runs."
    )


def test_dbt_project_yml_declares_external_materialization_default():
    """All 5 silver models default to ``materialized: external``.

    Pins decision #5 of the D3.0 plan doc + the D3.0-fu1 sandbox
    materialization-fork resolution: external + parameterized
    ``location`` is the path that produces partitioned Parquet output
    matching `silver/v1/<table>/utc_date=YYYY-MM-DD/data.parquet`.
    A future revert of this default (e.g., to ``table`` or
    ``incremental``) silently changes the write semantics + Parquet
    output path; this test fails loudly.
    """
    import yaml
    p = SILVER_DIR / "dbt_project.yml"
    if not p.is_file():
        pytest.skip("dbt_project.yml missing — covered by sibling test")
    data = yaml.safe_load(p.read_text())
    models_cfg = data.get("models", {}).get("silver", {})
    assert models_cfg.get("+materialized") == "external", (
        f"silver project-level +materialized must be 'external'; got "
        f"{models_cfg.get('+materialized')!r}. Decision #5 (per D3.0-fu1 "
        "sandbox): external + parameterized location is the materialization "
        "story for silver."
    )


def test_profiles_yml_example_exists():
    """``silver/profiles.yml.example`` is the operator-facing profile
    template — referenced by silver/README.md operator setup.
    """
    p = SILVER_DIR / "profiles.yml.example"
    assert p.is_file(), (
        f"{p} missing. D3.0-fu1 ships this as the operator-facing dbt "
        "profile documentation; the runtime wrapper writes its own "
        "tmp profiles.yml so this example is purely for operator reference."
    )


# ─── 2. Macros ──────────────────────────────────────────────────────────────


def test_envelope_columns_macro_exists():
    """``silver/macros/envelope_columns.sql`` defines the envelope_cols +
    bronze_glob + bronze_columns macros shared by all 5 models.

    Per plan-doc decision #7 (the "shared dbt macro" pin): all 5 silver
    models must project the same 7 envelope columns. Centralizing the
    projection in a macro prevents column-name drift across models.
    """
    p = SILVER_DIR / "macros" / "envelope_columns.sql"
    assert p.is_file(), (
        f"{p} missing. D3.0-fu1 / plan-doc decision #7 / R1-M2 retract: "
        "the envelope-col projection must be a shared macro to prevent "
        "column-name drift across the 5 Tier-1 models."
    )
    src = p.read_text()
    # Macro definitions are unconditional — silver_coinbase_ticker.sql et al
    # all reference these three by name.
    for macro_name in ("envelope_cols", "bronze_glob", "bronze_columns"):
        assert f"macro {macro_name}" in src, (
            f"macro `{macro_name}` not defined in {p}. "
            "All 5 silver/models/*.sql files reference these three macros; "
            "removing any one breaks dbt compilation."
        )


# ─── 3. Model files ─────────────────────────────────────────────────────────


# (vendor, model_name, silver_table) — matches the parametrize key names below.
# Pins the 5 Tier-1 silver models per plan-doc decisions #3 + R1-C1
# (channel _v2 suffix preserved for kalshi_market_lifecycle_v2).
SILVER_MODELS: tuple[tuple[str, str, str], ...] = (
    ("coinbase", "silver_coinbase_ticker",                "coinbase_ticker"),
    ("coinbase", "silver_coinbase_matches",               "coinbase_matches"),
    ("coinbase", "silver_coinbase_heartbeat",             "coinbase_heartbeat"),
    ("coinbase", "silver_coinbase_status",                "coinbase_status"),
    ("kalshi",   "silver_kalshi_market_lifecycle_v2",     "kalshi_market_lifecycle_v2"),
)


@pytest.mark.parametrize("vendor,model_name,silver_table", SILVER_MODELS)
def test_silver_model_sql_file_exists(vendor: str, model_name: str, silver_table: str):
    """Each of the 5 Tier-1 silver models has a .sql file at the expected
    path under ``silver/models/<vendor>/``.

    Renaming a model breaks ``--select <model_name>`` invocation from
    the wrapper, which would silently no-op (dbt prints "no models
    selected" but exits 0). This test is the cheap structural pin.
    """
    p = SILVER_DIR / "models" / vendor / f"{model_name}.sql"
    assert p.is_file(), (
        f"{p} missing. D3.0-fu1 ships 5 Tier-1 silver models under "
        f"silver/models/{vendor}/; removing or renaming requires updating "
        "this contract + TIER_1_SOURCES in silver/scripts/etl_run.py "
        "+ the integration tests in lockstep."
    )


@pytest.mark.parametrize("vendor,model_name,silver_table", SILVER_MODELS)
def test_silver_model_declares_external_location(vendor: str, model_name: str, silver_table: str):
    """Each silver model's config(...) declares materialized='external' +
    a ``location`` parameterized by ``var('silver_root')`` + ``var('target_date')``.

    Catches the "model file exists but config drift" failure mode where
    a model is materialized as ``table`` (data goes to DuckDB catalog,
    not Parquet) or its location string drops the var() substitution
    (writes all dates to the same file).
    """
    p = SILVER_DIR / "models" / vendor / f"{model_name}.sql"
    if not p.is_file():
        pytest.skip(f"{p} missing — covered by sibling test")
    src = p.read_text()
    assert "materialized='external'" in src or "materialized=\"external\"" in src, (
        f"{p} must declare materialized='external' in config(). "
        "Other materializations route through the DuckDB catalog, not "
        "the parameterized Parquet path silver/v1/<table>/utc_date=.../data.parquet."
    )
    assert "var('silver_root')" in src or "var(\"silver_root\")" in src, (
        f"{p} location must reference var('silver_root') so the wrapper can "
        "redirect output between local-fs (tests) and S3 (prod)."
    )
    assert "var('target_date')" in src or "var(\"target_date\")" in src, (
        f"{p} location must reference var('target_date') so each nightly "
        "run writes to its own utc_date= partition (idempotency model)."
    )
    assert silver_table in src, (
        f"{p} location must include the silver_table segment '{silver_table}' "
        f"so the output lands at silver/v1/{silver_table}/utc_date=.../."
    )


# ─── 4. Wrapper ↔ dbt model alignment ───────────────────────────────────────


def test_tier_1_sources_matches_model_files():
    """``silver.scripts.etl_run.TIER_1_SOURCES`` 4-tuple list matches the
    set of dbt model files under silver/models/.

    If the wrapper's TIER_1_SOURCES drifts from the on-disk model files
    (a model is added but the wrapper doesn't --select it, or vice versa),
    nightly runs silently skip data. This test fires on the first drift.
    """
    try:
        from silver.scripts import etl_run
    except ImportError as exc:
        pytest.skip(f"silver.scripts.etl_run not importable: {exc}")

    wrapper_model_names = {dbt_model for _, _, _, dbt_model in etl_run.TIER_1_SOURCES}
    on_disk_model_names = {
        p.stem
        for p in (SILVER_DIR / "models").rglob("*.sql")
    }
    assert wrapper_model_names == on_disk_model_names, (
        f"silver.scripts.etl_run.TIER_1_SOURCES dbt_model names drift "
        f"from on-disk silver/models/*.sql:\n"
        f"  wrapper: {sorted(wrapper_model_names)}\n"
        f"  on-disk: {sorted(on_disk_model_names)}\n"
        "If you added a model, extend TIER_1_SOURCES + bronze pre-check; "
        "if you removed one, drop from both surfaces."
    )


def test_etl_run_subprocesses_dbt_not_direct_duckdb():
    """``silver.scripts.etl_run`` invokes dbt via subprocess (the D3.0-fu1
    refactor) rather than executing DuckDB SQL directly (the D3.0 path).

    Behavioral guard against accidental revert of the D3.0-fu1 dbt
    refactor. The integration tests verify end-to-end output but do NOT
    distinguish "wrapper calls dbt" from "wrapper runs SQL directly" —
    both produce equivalent Parquet. This test pins the dbt subprocess
    path so a revert can't slip through behavioral tests alone.
    """
    import ast
    p = SILVER_DIR / "scripts" / "etl_run.py"
    assert p.is_file()
    src = p.read_text()
    tree = ast.parse(src)
    has_subprocess_run = False
    has_dbt_token = "dbt" in src.lower()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "run":
            if isinstance(node.value, ast.Name) and node.value.id == "subprocess":
                has_subprocess_run = True
    assert has_subprocess_run, (
        "silver/scripts/etl_run.py must call subprocess.run(...) — D3.0-fu1 "
        "refactored ETL to dbt subprocess. Reverting to direct DuckDB SQL "
        "execution would pass the integration tests but break the dbt "
        "project layering this Bit established."
    )
    assert has_dbt_token, (
        "silver/scripts/etl_run.py must reference dbt — D3.0-fu1 refactor "
        "subprocesses `dbt run`. Reverting violates the dbt-layering contract."
    )
