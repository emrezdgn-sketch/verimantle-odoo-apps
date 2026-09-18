# -*- coding: utf-8 -*-
"""Static guard: the tools module must contain no write.

REPLACES_PROVENANCE_RISK: attribution only - see PROVENANCE_AUDIT_v0.1.md.
Nothing in the mechanism below originates outside this repository: the
forbidden-call list is Odoo's own ORM vocabulary, and the scanner reads this
module's own source. Only the docstrings naming a prior suite were removed.

The claim this enforces is the one published in README.md: there is no sudo()
anywhere in the tool or controller paths, and a static test scans the source on
every run to keep it that way. Scanning source is the strongest form that claim
can take:

  * The XML-RPC proof (devtools/prove_readonly.py) shows Odoo refuses writes
    at runtime for the agent user.
  * The controller test shows the endpoint does not escalate.
  * This shows the code does not even ask. A write that Odoo refuses still
    produces an error the user sees; better for it never to be written.

Two of these tests exist to keep the scanner honest. A scanner that matches
nothing passes silently forever, which is worse than having none - so one test
feeds it a known violation and requires it to complain, and another pins the
size of the exemption list so it cannot quietly grow.
"""

import pathlib
import re

from odoo.tests import TransactionCase, tagged

# Calls that reach the database. Matched on the attribute name, which is how
# they appear in Odoo code regardless of the recordset they hang off.
FORBIDDEN = [
    (r"\.create\s*\(", "create()"),
    (r"\.write\s*\(", "write()"),
    (r"\.unlink\s*\(", "unlink()"),
    (r"\.sudo\s*\(", "sudo()"),
    (r"\._write\s*\(", "_write()"),
    (r"\.flush_recordset\s*\(", "flush_recordset()"),
    (r"\.execute\s*\(", "cr.execute()"),
    (r"_cr\.", "raw cursor"),
    (r"\.copy\s*\(", "copy()"),
]

# Paths under the module that must stay write-free, relative to the module root.
GUARDED = ["tools", "controllers"]

# The agent is allowed to write exactly two things: its own audit row and a
# proposal awaiting human approval. Neither is a business record, and both
# exist so that it does not have to touch one. A write is accepted only when
# the same line names one of these models - which keeps the allowance narrow
# and readable, rather than exempting a whole file.
WRITABLE_MODELS = ("mcp.tool.call", "mcp.proposal")

# Nothing is exempt today, and that is the point: if this list ever gains an
# entry, the test below that pins its length fails and somebody has to say why
# in a review rather than in a commit nobody reads.
EXEMPT = set()

MODULE_ROOT = pathlib.Path(__file__).resolve().parent.parent


def scan(paths, module_root=MODULE_ROOT):
    """:returns: list of (relative path, line number, what was found, the line)"""
    findings = []
    for name in paths:
        base = module_root / name
        if not base.exists():
            continue
        for source in sorted(base.rglob("*.py")):
            relative = source.relative_to(module_root).as_posix()
            if relative in EXEMPT:
                continue
            for number, line in enumerate(
                source.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue          # a comment naming a call is not a call
                if any(model in line for model in WRITABLE_MODELS):
                    continue          # the agent's own audit row or proposal
                for pattern, label in FORBIDDEN:
                    if re.search(pattern, line):
                        findings.append((relative, number, label, stripped))
    return findings


@tagged("post_install", "-at_install")
class TestReadOnlyGuarantee(TransactionCase):

    def test_the_tools_contain_no_write(self):
        """The guarantee itself: nothing under tools/ or controllers/ calls
        anything that reaches the database."""
        findings = scan(GUARDED)

        if findings:
            report = "\n".join(
                "  %s:%s  %s\n      %s" % row for row in findings
            )
            self.fail(
                "The read-only modules must not call anything that writes.\n"
                "%d finding(s):\n%s\n\n"
                "If one of these is genuinely safe, it does not belong here - "
                "a write belongs in an mcp.proposal record instead."
                % (len(findings), report)
            )

    def test_the_scanner_is_not_a_no_op(self):
        """A scanner that matches nothing would pass this suite forever while the
        guarantee rotted. Feed it a real violation and require a complaint.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            fake_root = pathlib.Path(directory)
            (fake_root / "tools").mkdir()
            (fake_root / "tools" / "offender.py").write_text(
                "def go(env):\n"
                "    env['mrp.production'].sudo().create({'x': 1})\n",
                encoding="utf-8",
            )

            findings = scan(["tools"], module_root=fake_root)

        labels = {label for _path, _line, label, _text in findings}
        self.assertIn("create()", labels)
        self.assertIn("sudo()", labels)

    def test_the_allowance_does_not_cover_business_models(self):
        """The audit row and the proposal are writable. Nothing else is.

        Without this, "allow writes on a line that names a model we trust"
        could quietly widen into "allow writes".
        """
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            fake_root = pathlib.Path(directory)
            (fake_root / "tools").mkdir()
            (fake_root / "tools" / "mixed.py").write_text(
                "def go(env):\n"
                "    env['mcp.tool.call'].create({'tool_name': 'x'})\n"
                "    env['mrp.production'].create({'product_id': 1})\n",
                encoding="utf-8",
            )

            findings = scan(["tools"], module_root=fake_root)

        self.assertEqual(len(findings), 1, findings)
        self.assertIn("mrp.production", findings[0][3])

    def test_the_scanner_ignores_comments(self):
        """Otherwise every explanation of why a write is forbidden would itself
        trip the scanner, and the fix would be to stop explaining."""
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            fake_root = pathlib.Path(directory)
            (fake_root / "tools").mkdir()
            (fake_root / "tools" / "documented.py").write_text(
                "# Never call .create() here - use a proposal.\n"
                "def go(env):\n"
                "    return env['mrp.bom'].search([])\n",
                encoding="utf-8",
            )

            findings = scan(["tools"], module_root=fake_root)

        self.assertEqual(findings, [])

    def test_the_exemption_list_has_not_grown(self):
        """The exemption list is the one place this guard could be widened
        quietly, so its size is pinned here."""
        self.assertEqual(
            EXEMPT, set(),
            "Something was exempted from the read-only scan. That is a product "
            "decision, not a refactor: say why in review before changing this."
        )

    def test_every_guarded_directory_actually_exists(self):
        """A typo in GUARDED would silently scan nothing and pass."""
        for name in GUARDED:
            self.assertTrue(
                (MODULE_ROOT / name).is_dir(),
                "guarded path %r does not exist - the scan covers nothing" % name,
            )

    def test_the_scan_covers_every_tool_source_file(self):
        """Pins the surface: a new tool file placed outside tools/ would escape
        the guard entirely."""
        scanned = {
            source.relative_to(MODULE_ROOT).as_posix()
            for name in GUARDED
            for source in (MODULE_ROOT / name).rglob("*.py")
        }
        expected = {
            "tools/__init__.py", "tools/common.py", "tools/readonly.py",
            "tools/registry.py", "tools/shortage.py",
            "controllers/__init__.py", "controllers/mcp.py",
        }
        self.assertEqual(
            scanned, expected,
            "The set of scanned files changed. Add the new file to this list "
            "deliberately, having checked it writes nothing."
        )
