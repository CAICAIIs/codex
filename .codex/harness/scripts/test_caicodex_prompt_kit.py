import json
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
PROMPT_KIT = REPO / "scripts" / "caicodex-prompt-kit"
CAICODEX = REPO / "scripts" / "caicodex"


def run(args, cwd=None, check=True):
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
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


class CaiCodexPromptKitTests(unittest.TestCase):
    def test_install_creates_prompt_modules_and_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)

            result = run([str(PROMPT_KIT), "--root", str(root), "install", "--json"])
            payload = json.loads(result.stdout)

            self.assertTrue((root / ".codex" / "prompt-kit" / "README.md").is_file())
            self.assertTrue((root / ".codex" / "prompt-kit" / "development.md").is_file())
            self.assertTrue(
                (root / ".codex" / "skills" / "caicodex-prompt-kit" / "SKILL.md").is_file()
            )
            skill_text = (
                root / ".codex" / "skills" / "caicodex-prompt-kit" / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertTrue(skill_text.startswith("---\n"))
            self.assertIn(".codex/prompt-kit/README.md", payload["files"])

            check = run([str(PROMPT_KIT), "--root", str(root), "check"])
            self.assertIn("prompt kit check passed", check.stdout)

    def test_install_refuses_to_overwrite_local_changes_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)

            run([str(PROMPT_KIT), "--root", str(root), "install"])
            readme = root / ".codex" / "prompt-kit" / "README.md"
            readme.write_text(readme.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")

            result = run([str(PROMPT_KIT), "--root", str(root), "install"], check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("file has local changes", result.stderr)

            forced = run([str(PROMPT_KIT), "--root", str(root), "install", "--force"])
            self.assertIn("written: .codex/prompt-kit/README.md", forced.stdout)

    def test_print_renders_chinese_prompt_with_task(self):
        result = run(
            [
                str(PROMPT_KIT),
                "print",
                "--mode",
                "debug",
                "--task",
                "修复登录超时",
            ],
            cwd=REPO,
        )

        self.assertIn("诊断调试", result.stdout)
        self.assertIn("修复登录超时", result.stdout)
        self.assertIn("减法检查", result.stdout)
        self.assertIn("验收路径", result.stdout)

    def test_caicodex_wrapper_delegates_prompt_kit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_repo(root)

            result = run(
                [
                    str(CAICODEX),
                    "prompt-kit",
                    "--root",
                    str(root),
                    "status",
                    "--json",
                ],
                cwd=root,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(Path(payload["root"]), root.resolve())
            self.assertFalse(payload["installed"])


if __name__ == "__main__":
    unittest.main()
