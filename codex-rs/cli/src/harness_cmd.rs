use anyhow::Context;
use anyhow::Result;
use anyhow::anyhow;
use anyhow::bail;
use clap::Parser;
use serde_json::Value;
use serde_json::json;
use std::fs;
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

use crate::harness_advisor_cmd::HarnessAdvisorCommand;
use crate::harness_advisor_cmd::run_harness_advisor_command;

const REQUIRED_FILES: &[&str] = &[
    "advisors/kimi-readonly-agent/agent.yaml",
    "advisors/kimi-readonly-agent/system.md",
    "advisors/empty-skills/.keep",
    "model-router.json",
    "model-router.schema.json",
    "provider-availability.json",
    "evals/task.schema.json",
    "scripts/run_advisor.py",
    "scripts/run_paired_benchmark.py",
    "scripts/compare_paired_benchmark.py",
    "scripts/run_eval_task.py",
    "scripts/validate_harness.py",
];

/// Inspect and run the project-level Codex harness.
#[derive(Debug, Parser)]
pub(crate) struct HarnessCommand {
    /// Directory to start searching from. Defaults to the current directory.
    #[arg(short = 'C', long = "cd", value_name = "DIR")]
    cwd: Option<PathBuf>,

    #[command(subcommand)]
    subcommand: HarnessSubcommand,
}

#[derive(Debug, clap::Subcommand)]
enum HarnessSubcommand {
    /// Show whether a .codex/harness directory is present and internally readable.
    Status(HarnessStatusCommand),

    /// Run the harness validator script.
    Validate(HarnessValidateCommand),

    /// List local eval tasks.
    Tasks(HarnessTasksCommand),

    /// Show the model routing summary.
    Router(HarnessRouterCommand),

    /// Run one eval task through the harness runner.
    Run(Box<HarnessRunCommand>),

    /// Ask a read-only external advisor to produce an auditable artifact.
    Advisor(HarnessAdvisorCommand),
}

#[derive(Debug, Parser)]
struct HarnessStatusCommand {
    /// Print machine-readable JSON.
    #[arg(long)]
    json: bool,
}

#[derive(Debug, Parser)]
struct HarnessValidateCommand {
    /// Python executable to use.
    #[arg(long, default_value = "python3")]
    python: String,
}

#[derive(Debug, Parser)]
struct HarnessTasksCommand {
    /// Print machine-readable JSON.
    #[arg(long)]
    json: bool,
}

#[derive(Debug, Parser)]
struct HarnessRouterCommand {
    /// Print machine-readable JSON.
    #[arg(long)]
    json: bool,
}

#[derive(Debug, Parser)]
struct HarnessRunCommand {
    /// Eval task id or JSON path.
    task: String,

    /// Per-command timeout in seconds.
    #[arg(long)]
    timeout: Option<u64>,

    /// Run setup commands before verifier commands.
    #[arg(long)]
    run_setup: bool,

    /// Optional JSONL output path.
    #[arg(long, value_name = "FILE")]
    output: Option<PathBuf>,

    /// Evaluation variant name, for example baseline, multimodel-lite, or proposed.
    #[arg(long)]
    variant: Option<String>,

    /// Run id this result should be compared against.
    #[arg(long = "baseline-run-id")]
    baseline_run_id: Option<String>,

    /// Human intervention count for this run.
    #[arg(long = "human-interventions")]
    human_interventions: Option<u64>,

    /// Token/API usage summary, if available.
    #[arg(long = "token-usage")]
    token_usage: Option<String>,

    /// Modified file count for this run.
    #[arg(long = "modified-files")]
    modified_files: Option<u64>,

    /// Reviewer finding count for this run.
    #[arg(long = "review-findings")]
    review_findings: Option<u64>,

    /// Failure category when the task fails.
    #[arg(long = "failure-category")]
    failure_category: Option<String>,

    /// Short run note.
    #[arg(long)]
    notes: Option<String>,

    /// Harness route id used for this run.
    #[arg(long = "route-id")]
    route_id: Option<String>,

    /// Primary model id used for this run.
    #[arg(long = "model-id")]
    model_id: Option<String>,

    /// Repeat index for frozen task comparisons.
    #[arg(long = "repeat-index")]
    repeat_index: Option<u64>,

    /// Reviewer finding disposition summary.
    #[arg(long = "finding-disposition")]
    finding_disposition: Option<String>,

    /// Prepare an isolated task workspace and run setup commands only.
    #[arg(long = "prepare-only")]
    prepare_only: bool,

    /// Run verifier commands against an already attempted workspace.
    #[arg(long = "verify-only")]
    verify_only: bool,

    /// Workspace path for prepare/verify benchmark tasks.
    #[arg(long)]
    workspace: Option<PathBuf>,

    /// Path to an agent attempt log or transcript.
    #[arg(long = "attempt-log")]
    attempt_log: Option<PathBuf>,

    /// Short note describing the agent attempt evidence.
    #[arg(long = "attempt-note")]
    attempt_note: Option<String>,

    /// Record this task as skipped without running commands.
    #[arg(long = "skip-reason")]
    skip_reason: Option<String>,

    /// Allow verifier commands that match the harness unsafe-command denylist.
    #[arg(long)]
    allow_unsafe: bool,

    /// Python executable to use.
    #[arg(long, default_value = "python3")]
    python: String,
}

#[derive(Debug, Clone)]
struct HarnessInspection {
    root: PathBuf,
    harness: PathBuf,
    missing: Vec<String>,
    tasks: Vec<TaskInfo>,
    router: RouterInfo,
}

#[derive(Debug, Clone)]
struct TaskInfo {
    id: String,
    title: Option<String>,
    category: Option<String>,
    risk_level: Option<String>,
    path: PathBuf,
}

#[derive(Debug, Clone, Default)]
struct RouterInfo {
    models: Vec<String>,
    routes: Vec<RouteInfo>,
    provider_availability: Vec<ProviderInfo>,
    remote_write_default: Option<String>,
}

#[derive(Debug, Clone)]
struct RouteInfo {
    id: String,
    enabled: bool,
    disabled_reason: Option<String>,
    advisors: Vec<String>,
}

#[derive(Debug, Clone)]
struct ProviderInfo {
    id: String,
    status: Option<String>,
    runtime_policy: Option<String>,
}

pub(crate) fn run_harness_command(cmd: HarnessCommand) -> Result<()> {
    let cwd = match cmd.cwd {
        Some(cwd) => cwd,
        None => std::env::current_dir().context("failed to read current directory")?,
    };
    let harness = find_harness(&cwd)
        .ok_or_else(|| anyhow!("no .codex/harness directory found from {}", cwd.display()))?;

    match cmd.subcommand {
        HarnessSubcommand::Status(status) => {
            let inspection = inspect_harness(&harness)?;
            print_status(&inspection, status.json)
        }
        HarnessSubcommand::Validate(validate) => run_validator(&harness, &validate.python),
        HarnessSubcommand::Tasks(tasks) => {
            let inspection = inspect_harness(&harness)?;
            print_tasks(&inspection, tasks.json)
        }
        HarnessSubcommand::Router(router) => {
            let inspection = inspect_harness(&harness)?;
            print_router(&inspection, router.json)
        }
        HarnessSubcommand::Run(run) => run_eval_task(&harness, *run),
        HarnessSubcommand::Advisor(advisor) => run_harness_advisor_command(&harness, advisor),
    }
}

fn find_harness(start: &Path) -> Option<PathBuf> {
    let mut current = start.to_path_buf();
    loop {
        let harness = current.join(".codex").join("harness");
        if harness.is_dir() {
            return Some(harness);
        }
        if !current.pop() {
            return None;
        }
    }
}

fn inspect_harness(harness: &Path) -> Result<HarnessInspection> {
    let root = harness
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| anyhow!("invalid harness path: {}", harness.display()))?
        .to_path_buf();
    let missing = REQUIRED_FILES
        .iter()
        .filter(|relative| !harness.join(relative).exists())
        .map(|relative| (*relative).to_string())
        .collect();
    let tasks = read_tasks(harness)?;
    let router = read_router(harness)?;

    Ok(HarnessInspection {
        root,
        harness: harness.to_path_buf(),
        missing,
        tasks,
        router,
    })
}

fn read_tasks(harness: &Path) -> Result<Vec<TaskInfo>> {
    let tasks_dir = harness.join("evals").join("tasks");
    if !tasks_dir.is_dir() {
        return Ok(Vec::new());
    }

    let mut tasks = Vec::new();
    for entry in fs::read_dir(&tasks_dir)
        .with_context(|| format!("failed to read {}", tasks_dir.display()))?
    {
        let path = entry?.path();
        if path.extension().and_then(|ext| ext.to_str()) != Some("json") {
            continue;
        }
        let value = read_json_file(&path)?;
        let Some(id) = value.get("id").and_then(Value::as_str) else {
            bail!("task {} is missing string field `id`", path.display());
        };
        tasks.push(TaskInfo {
            id: id.to_string(),
            title: value
                .get("title")
                .and_then(Value::as_str)
                .map(str::to_string),
            category: value
                .get("category")
                .and_then(Value::as_str)
                .map(str::to_string),
            risk_level: value
                .get("risk_level")
                .and_then(Value::as_str)
                .map(str::to_string),
            path,
        });
    }
    tasks.sort_by(|left, right| left.id.cmp(&right.id));
    Ok(tasks)
}

fn read_router(harness: &Path) -> Result<RouterInfo> {
    let router_path = harness.join("model-router.json");
    if !router_path.exists() {
        return Ok(RouterInfo::default());
    }

    let value = read_json_file(&router_path)?;
    let models = value
        .get("models")
        .and_then(Value::as_object)
        .map(|models| {
            let mut keys: Vec<_> = models.keys().cloned().collect();
            keys.sort();
            keys
        })
        .unwrap_or_default();
    let routes = value
        .get("routes")
        .and_then(Value::as_array)
        .map(|routes| {
            routes
                .iter()
                .filter_map(|route| {
                    let id = route.get("id").and_then(Value::as_str)?.to_string();
                    let advisors = route
                        .get("advisors")
                        .and_then(Value::as_array)
                        .map(|advisors| {
                            advisors
                                .iter()
                                .filter_map(Value::as_str)
                                .map(str::to_string)
                                .collect()
                        })
                        .unwrap_or_default();
                    Some(RouteInfo {
                        id,
                        enabled: route
                            .get("enabled")
                            .and_then(Value::as_bool)
                            .unwrap_or(true),
                        disabled_reason: route
                            .get("disabled_reason")
                            .and_then(Value::as_str)
                            .map(str::to_string),
                        advisors,
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    let provider_availability = read_provider_availability(harness)?;
    let remote_write_default = value
        .get("remote_write_policy")
        .and_then(|policy| policy.get("default"))
        .and_then(Value::as_str)
        .map(str::to_string);

    Ok(RouterInfo {
        models,
        routes,
        provider_availability,
        remote_write_default,
    })
}

fn read_provider_availability(harness: &Path) -> Result<Vec<ProviderInfo>> {
    let availability_path = harness.join("provider-availability.json");
    if !availability_path.exists() {
        return Ok(Vec::new());
    }

    let value = read_json_file(&availability_path)?;
    let providers = value
        .get("providers")
        .and_then(Value::as_object)
        .map(|providers| {
            let mut entries: Vec<_> = providers
                .iter()
                .map(|(id, provider)| ProviderInfo {
                    id: id.clone(),
                    status: provider
                        .get("status")
                        .and_then(Value::as_str)
                        .map(str::to_string),
                    runtime_policy: provider
                        .get("runtime_policy")
                        .and_then(Value::as_str)
                        .map(str::to_string),
                })
                .collect();
            entries.sort_by(|left, right| left.id.cmp(&right.id));
            entries
        })
        .unwrap_or_default();
    Ok(providers)
}

fn read_json_file(path: &Path) -> Result<Value> {
    let text = fs::read_to_string(path)
        .with_context(|| format!("failed to read JSON file {}", path.display()))?;
    serde_json::from_str(&text).with_context(|| format!("invalid JSON in {}", path.display()))
}

fn print_status(inspection: &HarnessInspection, json_output: bool) -> Result<()> {
    let ok = inspection.missing.is_empty();
    if json_output {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "ok": ok,
                "root": inspection.root,
                "harness": inspection.harness,
                "missing": inspection.missing,
                "task_count": inspection.tasks.len(),
                "model_count": inspection.router.models.len(),
                "route_count": inspection.router.routes.len(),
                "enabled_route_count": inspection.router.routes.iter().filter(|route| route.enabled).count(),
                "remote_write_default": inspection.router.remote_write_default,
            }))?
        );
        return Ok(());
    }

    println!("Harness: {}", inspection.harness.display());
    println!("Status: {}", if ok { "OK" } else { "MISSING FILES" });
    println!("Tasks: {}", inspection.tasks.len());
    println!("Models: {}", inspection.router.models.len());
    println!("Routes: {}", inspection.router.routes.len());
    println!(
        "Enabled routes: {}",
        inspection
            .router
            .routes
            .iter()
            .filter(|route| route.enabled)
            .count()
    );
    if let Some(policy) = &inspection.router.remote_write_default {
        println!("Remote writes: {policy}");
    }
    if !inspection.missing.is_empty() {
        println!("Missing:");
        for path in &inspection.missing {
            println!("  - {path}");
        }
    }
    Ok(())
}

fn print_tasks(inspection: &HarnessInspection, json_output: bool) -> Result<()> {
    if json_output {
        let tasks: Vec<_> = inspection
            .tasks
            .iter()
            .map(|task| {
                json!({
                    "id": task.id,
                    "title": task.title,
                    "category": task.category,
                    "risk_level": task.risk_level,
                    "path": task.path,
                })
            })
            .collect();
        println!("{}", serde_json::to_string_pretty(&tasks)?);
        return Ok(());
    }

    for task in &inspection.tasks {
        let title = task.title.as_deref().unwrap_or("");
        let category = task.category.as_deref().unwrap_or("unknown");
        let risk = task.risk_level.as_deref().unwrap_or("unknown");
        println!("{} [{category}/{risk}] {title}", task.id);
    }
    Ok(())
}

fn print_router(inspection: &HarnessInspection, json_output: bool) -> Result<()> {
    if json_output {
        let routes: Vec<_> = inspection
            .router
            .routes
            .iter()
            .map(|route| {
                json!({
                    "id": &route.id,
                    "enabled": route.enabled,
                    "disabled_reason": &route.disabled_reason,
                    "advisors": &route.advisors,
                })
            })
            .collect();
        let providers: Vec<_> = inspection
            .router
            .provider_availability
            .iter()
            .map(|provider| {
                json!({
                    "id": &provider.id,
                    "status": &provider.status,
                    "runtime_policy": &provider.runtime_policy,
                })
            })
            .collect();
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({
                "models": inspection.router.models,
                "routes": routes,
                "provider_availability": providers,
                "remote_write_default": inspection.router.remote_write_default,
            }))?
        );
        return Ok(());
    }

    println!("Models:");
    for model in &inspection.router.models {
        println!("  - {model}");
    }
    println!("Routes:");
    for route in &inspection.router.routes {
        if route.enabled {
            println!("  - {} (enabled)", route.id);
        } else {
            let reason = route
                .disabled_reason
                .as_deref()
                .unwrap_or("no reason recorded");
            println!("  - {} (disabled: {reason})", route.id);
        }
    }
    if !inspection.router.provider_availability.is_empty() {
        println!("Provider availability:");
        for provider in &inspection.router.provider_availability {
            let status = provider.status.as_deref().unwrap_or("unknown");
            let policy = provider
                .runtime_policy
                .as_deref()
                .unwrap_or("unknown-policy");
            println!("  - {}: {status} ({policy})", provider.id);
        }
    }
    if let Some(policy) = &inspection.router.remote_write_default {
        println!("Remote writes: {policy}");
    }
    Ok(())
}

fn run_validator(harness: &Path, python: &str) -> Result<()> {
    let script = harness.join("scripts").join("validate_harness.py");
    if !script.exists() {
        bail!("missing harness validator: {}", script.display());
    }
    run_python_script(harness, python, &[script.into_os_string()])
}

fn run_eval_task(harness: &Path, args: HarnessRunCommand) -> Result<()> {
    let script = harness.join("scripts").join("run_eval_task.py");
    if !script.exists() {
        bail!("missing harness runner: {}", script.display());
    }

    let mut command_args = vec![script.into_os_string(), args.task.into()];
    if let Some(timeout) = args.timeout {
        command_args.push("--timeout".into());
        command_args.push(timeout.to_string().into());
    }
    if args.run_setup {
        command_args.push("--run-setup".into());
    }
    if let Some(output) = args.output {
        command_args.push("--output".into());
        command_args.push(output.into_os_string());
    }
    if let Some(variant) = args.variant {
        command_args.push("--variant".into());
        command_args.push(variant.into());
    }
    if let Some(baseline_run_id) = args.baseline_run_id {
        command_args.push("--baseline-run-id".into());
        command_args.push(baseline_run_id.into());
    }
    if let Some(human_interventions) = args.human_interventions {
        command_args.push("--human-interventions".into());
        command_args.push(human_interventions.to_string().into());
    }
    if let Some(token_usage) = args.token_usage {
        command_args.push("--token-usage".into());
        command_args.push(token_usage.into());
    }
    if let Some(modified_files) = args.modified_files {
        command_args.push("--modified-files".into());
        command_args.push(modified_files.to_string().into());
    }
    if let Some(review_findings) = args.review_findings {
        command_args.push("--review-findings".into());
        command_args.push(review_findings.to_string().into());
    }
    if let Some(failure_category) = args.failure_category {
        command_args.push("--failure-category".into());
        command_args.push(failure_category.into());
    }
    if let Some(notes) = args.notes {
        command_args.push("--notes".into());
        command_args.push(notes.into());
    }
    if let Some(route_id) = args.route_id {
        command_args.push("--route-id".into());
        command_args.push(route_id.into());
    }
    if let Some(model_id) = args.model_id {
        command_args.push("--model-id".into());
        command_args.push(model_id.into());
    }
    if let Some(repeat_index) = args.repeat_index {
        command_args.push("--repeat-index".into());
        command_args.push(repeat_index.to_string().into());
    }
    if let Some(finding_disposition) = args.finding_disposition {
        command_args.push("--finding-disposition".into());
        command_args.push(finding_disposition.into());
    }
    if args.prepare_only {
        command_args.push("--prepare-only".into());
    }
    if args.verify_only {
        command_args.push("--verify-only".into());
    }
    if let Some(workspace) = args.workspace {
        command_args.push("--workspace".into());
        command_args.push(workspace.into_os_string());
    }
    if let Some(attempt_log) = args.attempt_log {
        command_args.push("--attempt-log".into());
        command_args.push(attempt_log.into_os_string());
    }
    if let Some(attempt_note) = args.attempt_note {
        command_args.push("--attempt-note".into());
        command_args.push(attempt_note.into());
    }
    if let Some(skip_reason) = args.skip_reason {
        command_args.push("--skip-reason".into());
        command_args.push(skip_reason.into());
    }
    if args.allow_unsafe {
        command_args.push("--allow-unsafe".into());
    }

    run_python_script(harness, &args.python, &command_args)
}

fn run_python_script(harness: &Path, python: &str, args: &[std::ffi::OsString]) -> Result<()> {
    let root = harness
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| anyhow!("invalid harness path: {}", harness.display()))?;
    let status = Command::new(python)
        .args(args)
        .current_dir(root)
        .status()
        .with_context(|| format!("failed to start {python}"))?;
    if !status.success() {
        bail!("harness command failed with {status}");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use pretty_assertions::assert_eq;
    use std::fs::create_dir_all;
    use std::fs::write;
    use tempfile::TempDir;

    fn write_minimal_harness(root: &Path) -> PathBuf {
        let harness = root.join(".codex").join("harness");
        create_dir_all(harness.join("evals").join("tasks")).unwrap();
        create_dir_all(harness.join("scripts")).unwrap();
        for path in REQUIRED_FILES {
            let full_path = harness.join(path);
            if full_path.exists() {
                continue;
            }
            if let Some(parent) = full_path.parent() {
                create_dir_all(parent).unwrap();
            }
            write(&full_path, "{}").unwrap();
        }
        write(
            harness.join("model-router.json"),
            r#"{
              "models": {"gpt-5.5": {}, "glm-5.1": {}},
              "routes": [{"id": "simple-local-change"}, {"id": "workflow-evaluation"}],
              "remote_write_policy": {"default": "forbid"}
            }"#,
        )
        .unwrap();
        write(
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
        )
        .unwrap();
        harness
    }

    #[test]
    fn find_harness_walks_up_from_nested_directory() {
        let temp = TempDir::new().unwrap();
        let harness = write_minimal_harness(temp.path());
        let nested = temp.path().join("a").join("b");
        create_dir_all(&nested).unwrap();

        assert_eq!(find_harness(&nested), Some(harness));
    }

    #[test]
    fn inspect_harness_reads_tasks_and_router() {
        let temp = TempDir::new().unwrap();
        let harness = write_minimal_harness(temp.path());

        let inspection = inspect_harness(&harness).unwrap();

        assert!(inspection.missing.is_empty());
        assert_eq!(inspection.tasks.len(), 1);
        assert_eq!(inspection.tasks[0].id, "harness-self-check");
        assert_eq!(inspection.router.models, vec!["glm-5.1", "gpt-5.5"]);
        let route_ids: Vec<_> = inspection
            .router
            .routes
            .iter()
            .map(|route| route.id.as_str())
            .collect();
        assert_eq!(
            route_ids,
            vec!["simple-local-change", "workflow-evaluation"]
        );
        assert!(inspection.router.routes.iter().all(|route| route.enabled));
        assert_eq!(
            inspection.router.remote_write_default.as_deref(),
            Some("forbid")
        );
    }
}
