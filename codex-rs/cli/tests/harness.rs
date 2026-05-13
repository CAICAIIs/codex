use std::path::Path;

use anyhow::Result;
use predicates::str::contains;
use pretty_assertions::assert_eq;
use tempfile::TempDir;

fn codex_command(codex_home: &Path) -> Result<assert_cmd::Command> {
    let mut cmd = assert_cmd::Command::new(codex_utils_cargo_bin::cargo_bin("codex")?);
    cmd.env("CODEX_HOME", codex_home);
    Ok(cmd)
}

fn write_project_harness(project: &Path) -> Result<()> {
    let harness = project.join(".codex").join("harness");
    std::fs::create_dir_all(harness.join("evals").join("tasks"))?;
    std::fs::create_dir_all(harness.join("scripts"))?;
    for path in [
        "README.md",
        "commands.md",
        "verification.md",
        "multi-model.md",
        "evaluation.md",
        "security.md",
        "advisors/kimi-readonly-agent/agent.yaml",
        "advisors/kimi-readonly-agent/system.md",
        "advisors/empty-skills/.keep",
        "model-router.schema.json",
        "provider-availability.json",
        "evals/task.schema.json",
        "scripts/run_advisor.py",
        "scripts/run_paired_benchmark.py",
        "scripts/compare_paired_benchmark.py",
        "scripts/run_eval_task.py",
        "scripts/validate_harness.py",
    ] {
        let full_path = harness.join(path);
        if let Some(parent) = full_path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(full_path, "")?;
    }
    std::fs::write(
        harness.join("model-router.json"),
        r#"{
          "models": {"gpt-5.5": {}},
          "routes": [{"id": "simple-local-change"}],
          "remote_write_policy": {"default": "forbid"}
        }"#,
    )?;
    std::fs::write(
        harness.join("provider-availability.json"),
        r#"{
          "providers": {}
        }"#,
    )?;
    std::fs::write(
        harness
            .join("evals")
            .join("tasks")
            .join("001-harness-self-check.json"),
        r#"{
          "id": "harness-self-check",
          "title": "Harness self check",
          "category": "workflow",
          "risk_level": "low"
        }"#,
    )?;
    Ok(())
}

#[test]
fn harness_advisor_passes_arguments_to_runner() -> Result<()> {
    let codex_home = TempDir::new()?;
    let project = TempDir::new()?;
    write_project_harness(project.path())?;
    let output = project.path().join("advisor-args.json");
    let script = project
        .path()
        .join(".codex")
        .join("harness")
        .join("scripts")
        .join("run_advisor.py");
    std::fs::write(
        script,
        r#"#!/usr/bin/env python3
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[sys.argv.index("--output") + 1])
path.write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
"#,
    )?;

    let mut cmd = codex_command(codex_home.path())?;
    cmd.current_dir(project.path())
        .args([
            "harness",
            "advisor",
            "glm",
            "--role",
            "critic",
            "--topic",
            "dry run",
            "--evidence",
            "README.md",
            "--output",
            output.to_str().expect("temp path is UTF-8"),
            "--dry-run",
        ])
        .assert()
        .success();

    let output_arg = output.to_string_lossy().into_owned();
    let args: Vec<String> = serde_json::from_str(&std::fs::read_to_string(output)?)?;
    assert_eq!(
        args,
        vec![
            "glm".to_string(),
            "--role".to_string(),
            "critic".to_string(),
            "--topic".to_string(),
            "dry run".to_string(),
            "--evidence".to_string(),
            "README.md".to_string(),
            "--output".to_string(),
            output_arg,
            "--dry-run".to_string(),
        ]
    );
    Ok(())
}

#[test]
fn harness_status_reports_project_harness() -> Result<()> {
    let codex_home = TempDir::new()?;
    let project = TempDir::new()?;
    write_project_harness(project.path())?;

    let mut cmd = codex_command(codex_home.path())?;
    let output = cmd
        .current_dir(project.path())
        .args(["harness", "status", "--json"])
        .assert()
        .success()
        .get_output()
        .stdout
        .clone();

    let value: serde_json::Value = serde_json::from_slice(&output)?;
    assert_eq!(value["ok"], true);
    assert_eq!(value["task_count"], 1);
    assert_eq!(value["model_count"], 1);
    assert_eq!(value["route_count"], 1);
    assert_eq!(value["remote_write_default"], "forbid");
    Ok(())
}

#[test]
fn harness_tasks_lists_task_ids() -> Result<()> {
    let codex_home = TempDir::new()?;
    let project = TempDir::new()?;
    write_project_harness(project.path())?;

    let mut cmd = codex_command(codex_home.path())?;
    cmd.current_dir(project.path())
        .args(["harness", "tasks"])
        .assert()
        .success()
        .stdout(contains(
            "harness-self-check [workflow/low] Harness self check",
        ));

    Ok(())
}
