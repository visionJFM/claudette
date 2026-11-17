# Single source for the leak guard's secret-shaped-content patterns.
#
# Read by push-diff.sh (POSIX ERE via grep -E) — the /claudette push flow, and the
# scan the claude_md_push_leak_scan rule runs before any push. Tests read it in
# Python, so keep every pattern valid in both dialects: no lookaround, no \d, no
# named groups.
#
# One pattern per line. Blank lines and # comments are ignored; the remaining
# lines are OR-ed together.
#
# Narrow and exact on purpose: a false positive that stops a push trains you to
# bypass the guard, so no *secret* / *token* style patterns.
-----BEGIN [A-Z ]*PRIVATE KEY-----
sk-ant-[A-Za-z0-9_-]{20}
ghp_[A-Za-z0-9]{30}
AKIA[0-9A-Z]{16}
xox[baprs]-[A-Za-z0-9-]{10}
