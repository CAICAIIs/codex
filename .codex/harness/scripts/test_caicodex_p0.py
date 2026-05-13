import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
HOOKIFY = REPO / "scripts" / "caicodex-hookify"
CHECKPOINT = REPO / "scripts" / "caicodex-checkpoint"


def run(args, cwd=None, input_text=None):
    return subprocess.run(
        args,
        cwd=cwd,
        input=input_text,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def init_repo(root: Path) -> None:
    run(["git", "init", "-q"], cwd=root)
    run(["git", "config", "user.email", "test@example.com"], cwd=root)
    run(["git", "config", "user.name", "Tester"], cwd=root)
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    run(["git", "add", "README.md"], cwd=root)
    run(["git", "commit", "-q", "-m", "init"], cwd=root)


class CaiCodexP0Tests(unittest.TestCase):
    def test_hookify_adds_rule_compiles_hook_and_blocks_matching_bash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)

            run(
                [
                    str(HOOKIFY),
                    "--root",
                    str(root),
                    "add",
                    "block-rm",
                    "--event",
                    "bash",
                    "--pattern",
                    r"rm\s+-rf",
                    "--action",
                    "block",
                    "--message",
                    "no rm",
                ]
            )

            hooks = json.loads((root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
            self.assertIn("PreToolUse", hooks["hooks"])

            payload = json.dumps(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "rm -rf /tmp/demo"},
                }
            )
            result = run([str(HOOKIFY), "--root", str(root), "run"], input_text=payload)
            parsed = json.loads(result.stdout)
            self.assertEqual(
                parsed,
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "block-rm: no rm",
                    }
                },
            )

    def test_hookify_defaults_block_common_codex_branch_creation_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)

            run([str(HOOKIFY), "--root", str(root), "defaults"])

            for command in [
                "git switch -c codex/demo",
                "git checkout -b codex/demo",
                "git branch codex/demo",
                "git update-ref refs/heads/codex/demo HEAD",
            ]:
                payload = json.dumps(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Bash",
                        "tool_input": {"command": command},
                    }
                )
                result = run([str(HOOKIFY), "--root", str(root), "run"], input_text=payload)
                parsed = json.loads(result.stdout)
                self.assertEqual(
                    parsed["hookSpecificOutput"]["permissionDecision"],
                    "deny",
                    command,
                )

    def test_hookify_compile_prunes_removed_generated_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)

            run(
                [
                    str(HOOKIFY),
                    "--root",
                    str(root),
                    "add",
                    "warn-prompt",
                    "--event",
                    "prompt",
                    "--pattern",
                    "hello",
                    "--message",
                    "seen",
                ]
            )
            run([str(HOOKIFY), "--root", str(root), "remove", "warn-prompt"])

            hooks_text = (root / ".codex" / "hooks.json").read_text(encoding="utf-8")
            self.assertNotIn("caicodex-hookify", hooks_text)

    def test_hookify_warn_returns_codex_system_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)

            run(
                [
                    str(HOOKIFY),
                    "--root",
                    str(root),
                    "add",
                    "warn-prompt",
                    "--event",
                    "prompt",
                    "--pattern",
                    "risky",
                    "--message",
                    "review first",
                ]
            )

            payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "do risky thing",
                }
            )
            result = run([str(HOOKIFY), "--root", str(root), "run"], input_text=payload)
            parsed = json.loads(result.stdout)
            self.assertEqual(
                parsed,
                {"systemMessage": "caicodex hookify warning:\nwarn-prompt: review first"},
            )

    def test_checkpoint_restore_round_trips_tracked_and_untracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)

            (root / "README.md").write_text("checkpoint state\n", encoding="utf-8")
            (root / "note.txt").write_text("keep me\n", encoding="utf-8")
            created = run(
                [
                    str(CHECKPOINT),
                    "--root",
                    str(root),
                    "create",
                    "--label",
                    "before",
                    "--json",
                ]
            )
            checkpoint_id = json.loads(created.stdout)["id"]

            (root / "README.md").write_text("bad state\n", encoding="utf-8")
            (root / "note.txt").write_text("bad note\n", encoding="utf-8")
            run([str(CHECKPOINT), "--root", str(root), "restore", checkpoint_id, "--force"])

            self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "checkpoint state\n")
            self.assertEqual((root / "note.txt").read_text(encoding="utf-8"), "keep me\n")

    def test_checkpoint_restore_preserves_index_worktree_and_tracked_deletions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)
            (root / "delete-me.txt").write_text("base\n", encoding="utf-8")
            run(["git", "add", "delete-me.txt"], cwd=root)
            run(["git", "commit", "-q", "-m", "add delete fixture"], cwd=root)

            (root / "README.md").write_text("staged\n", encoding="utf-8")
            run(["git", "add", "README.md"], cwd=root)
            (root / "README.md").write_text("unstaged\n", encoding="utf-8")
            (root / "delete-me.txt").unlink()
            created = run(
                [
                    str(CHECKPOINT),
                    "--root",
                    str(root),
                    "create",
                    "--label",
                    "mixed-state",
                    "--json",
                ]
            )
            checkpoint_id = json.loads(created.stdout)["id"]

            run(["git", "restore", "--source", "HEAD", "--staged", "--worktree", "--", "."], cwd=root)
            (root / "README.md").write_text("bad\n", encoding="utf-8")
            (root / "delete-me.txt").write_text("bad\n", encoding="utf-8")
            run([str(CHECKPOINT), "--root", str(root), "restore", checkpoint_id, "--force"])

            status = run(["git", "status", "--porcelain=v1"], cwd=root).stdout
            self.assertIn("MM README.md", status)
            self.assertIn(" D delete-me.txt", status)
            self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "unstaged\n")
            self.assertFalse((root / "delete-me.txt").exists())

    def test_checkpoint_restore_rejects_head_mismatch_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)

            (root / "README.md").write_text("checkpoint state\n", encoding="utf-8")
            created = run(
                [
                    str(CHECKPOINT),
                    "--root",
                    str(root),
                    "create",
                    "--label",
                    "before-new-commit",
                    "--json",
                ]
            )
            checkpoint_id = json.loads(created.stdout)["id"]
            run(["git", "restore", "--source", "HEAD", "--staged", "--worktree", "--", "."], cwd=root)
            (root / "later.txt").write_text("later\n", encoding="utf-8")
            run(["git", "add", "later.txt"], cwd=root)
            run(["git", "commit", "-q", "-m", "later"], cwd=root)

            result = subprocess.run(
                [str(CHECKPOINT), "--root", str(root), "restore", checkpoint_id],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("checkpoint HEAD does not match current HEAD", result.stderr)

    def test_checkpoint_hook_and_hookify_can_share_hooks_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)

            run([str(CHECKPOINT), "--root", str(root), "install-hook"])
            run([str(HOOKIFY), "--root", str(root), "defaults"])

            hooks = json.loads((root / ".codex" / "hooks.json").read_text(encoding="utf-8"))
            self.assertIn("UserPromptSubmit", hooks["hooks"])
            self.assertIn("PreToolUse", hooks["hooks"])


if __name__ == "__main__":
    unittest.main()
