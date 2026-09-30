"""Static checks for the standalone Stage 4 image layout."""
from pathlib import Path
import unittest


STAGE4 = Path(__file__).resolve().parents[1]


class PackageLayout(unittest.TestCase):
    def test_image_copies_application_assets_and_contract_tests(self):
        dockerfile = (STAGE4 / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY app ./app", dockerfile)
        self.assertIn("COPY tests ./tests", dockerfile)
        for asset in ("index.html", "app.js", "app.css"):
            with self.subTest(asset=asset):
                self.assertTrue((STAGE4 / "app" / "web" / asset).is_file())
        self.assertTrue((STAGE4 / "tests" / "contract_stage1.py").is_file())
        self.assertTrue((STAGE4 / "tests" / "test_core_state.py").is_file())

    def test_image_has_offline_runtime_prerequisites_and_safe_defaults(self):
        dockerfile = (STAGE4 / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("python:3.12-slim", dockerfile)
        self.assertIn("DEBIAN_FRONTEND=noninteractive apt-get install", dockerfile)
        self.assertIn("tzdata", dockerfile)
        self.assertIn("USER 10001", dockerfile)
        self.assertIn("PORT=8080", dockerfile)
        self.assertIn("EXPOSE 8080", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertIn("CMD [\"python\", \"-m\", \"app\"]", dockerfile)
        self.assertNotIn("pip install", dockerfile.lower())
        self.assertNotIn("npm install", dockerfile.lower())

    def test_build_context_excludes_generated_and_vcs_files(self):
        ignored = (STAGE4 / ".dockerignore").read_text(encoding="utf-8").splitlines()
        for pattern in ("**/.git", "**/.venv", "**/__pycache__", "**/*.py[cod]"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignored)

    def test_stage4_run_guide_matches_standalone_layout(self):
        guide = (STAGE4 / "RUN.md").read_text(encoding="utf-8")
        self.assertIn("Tablekeeper Stage 4", guide)
        self.assertIn("from the `stage-4` directory", guide)
        self.assertIn("tablekeeper-stage4", guide)
        self.assertIn("replans", guide)
        self.assertIn("series", guide)
        self.assertIn("closures", guide)
        self.assertNotIn("stage-3 directory", guide)
        self.assertNotIn("tablekeeper-stage3", guide)

        workflow = STAGE4.parents[0] / ".github" / "workflows" / "stage4-harness.yml"
        content = workflow.read_text(encoding="utf-8")
        self.assertIn("--track tablekeeper", content)
        self.assertIn("--stage 4", content)
        self.assertIn("--mode isolated", content)
        self.assertIn("803560d2a678ace1414465c098eb0ab5380ffade", content)
        self.assertIn('exit $EXIT_CODE', content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
