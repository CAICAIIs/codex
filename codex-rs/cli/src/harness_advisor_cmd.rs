use anyhow::Context;
use anyhow::Result;
use anyhow::bail;
use clap::Parser;
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

#[derive(Debug, Parser)]
pub(crate) struct HarnessAdvisorCommand {
    /// Provider id, for example glm or kimi-cli.
    provider: String,

    /// Advisor role to use.
    #[arg(long, default_value = "critic")]
    role: String,

    /// Topic or question for the advisor.
    #[arg(long)]
    topic: String,

    /// Evidence file to include. Can be passed multiple times.
    #[arg(long, value_name = "FILE")]
    evidence: Vec<PathBuf>,

    /// Optional JSON artifact output path.
    #[arg(long, value_name = "FILE")]
    output: Option<PathBuf>,

    /// Request timeout in seconds.
    #[arg(long)]
    timeout: Option<u64>,

    /// Maximum provider output tokens.
    #[arg(long = "max-tokens")]
    max_tokens: Option<u64>,

    /// Maximum characters to include per evidence file.
    #[arg(long = "max-chars-per-file")]
    max_chars_per_file: Option<u64>,

    /// Maximum total evidence characters.
    #[arg(long = "max-total-chars")]
    max_total_chars: Option<u64>,

    /// Build the prompt and artifact shape without calling a provider.
    #[arg(long)]
    dry_run: bool,

    /// Python executable to use.
    #[arg(long, default_value = "python3")]
    python: String,
}

pub(crate) fn run_harness_advisor_command(
    harness: &Path,
    args: HarnessAdvisorCommand,
) -> Result<()> {
    let script = harness.join("scripts").join("run_advisor.py");
    if !script.exists() {
        bail!("missing harness advisor runner: {}", script.display());
    }

    let mut command_args = vec![script.into_os_string(), args.provider.into()];
    command_args.push("--role".into());
    command_args.push(args.role.into());
    command_args.push("--topic".into());
    command_args.push(args.topic.into());
    for evidence in args.evidence {
        command_args.push("--evidence".into());
        command_args.push(evidence.into_os_string());
    }
    if let Some(output) = args.output {
        command_args.push("--output".into());
        command_args.push(output.into_os_string());
    }
    if let Some(timeout) = args.timeout {
        command_args.push("--timeout".into());
        command_args.push(timeout.to_string().into());
    }
    if let Some(max_tokens) = args.max_tokens {
        command_args.push("--max-tokens".into());
        command_args.push(max_tokens.to_string().into());
    }
    if let Some(max_chars_per_file) = args.max_chars_per_file {
        command_args.push("--max-chars-per-file".into());
        command_args.push(max_chars_per_file.to_string().into());
    }
    if let Some(max_total_chars) = args.max_total_chars {
        command_args.push("--max-total-chars".into());
        command_args.push(max_total_chars.to_string().into());
    }
    if args.dry_run {
        command_args.push("--dry-run".into());
    }

    run_python_script(harness, &args.python, &command_args)
}

fn run_python_script(harness: &Path, python: &str, args: &[std::ffi::OsString]) -> Result<()> {
    let root = harness
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| anyhow::anyhow!("invalid harness path: {}", harness.display()))?;
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
