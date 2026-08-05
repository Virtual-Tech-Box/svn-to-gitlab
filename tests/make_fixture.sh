#!/bin/sh
# Build a Subversion repository exercising the cases the migration has to handle:
# standard layout, several authors, branches, tags (pure copies and modified copies),
# svn:ignore, a binary file, a non-ASCII path, and a deleted branch.
set -eu

REPO="$1"
WC="$2"

rm -rf "$REPO" "$WC"
svnadmin create "$REPO"

# Allow revision property changes so we can rewrite authors into the fixture.
cat > "$REPO/hooks/pre-revprop-change" <<'HOOK'
#!/bin/sh
exit 0
HOOK
chmod +x "$REPO/hooks/pre-revprop-change"

URL="file://$REPO"
svn mkdir -q -m "Create standard layout" "$URL/trunk" "$URL/branches" "$URL/tags"
svn checkout -q "$URL/trunk" "$WC"

cd "$WC"
mkdir -p src docs "resources/données"
printf 'print("hello")\n'            > src/app.py
printf '# Project\n\nInitial docs.\n' > docs/README.md
printf 'accent test\n'                > "resources/données/notes.txt"
head -c 200000 /dev/urandom           > resources/blob.bin
svn add -q src docs resources
svn propset -q svn:ignore "*.pyc
build/
*.log" .
svn propset -q svn:mime-type application/octet-stream resources/blob.bin
svn commit -q -m "Initial import of the application"

printf 'print("hello, world")\n' > src/app.py
svn commit -q -m "Improve the greeting"

printf 'def helper():\n    return 42\n' > src/helper.py
svn add -q src/helper.py
svn commit -q -m "Add a helper function"

cd ..
# A branch with its own commits.
svn copy -q -m "Branch for the 1.x line" "$URL/trunk" "$URL/branches/release-1.x"
svn checkout -q "$URL/branches/release-1.x" wc-branch
cd wc-branch
printf 'VERSION = "1.0.1"\n' > src/version.py
svn add -q src/version.py
svn commit -q -m "Set the 1.0.1 version on the release branch"
cd ..

# A pure-copy tag: the tag commit changes nothing, so it should be retargeted to
# its parent when convert.tag_from_parent is enabled.
svn copy -q -m "Tag 1.0.0" "$URL/trunk" "$URL/tags/v1.0.0"

# A tag that was modified after creation: must keep pointing at the modified state.
svn copy -q -m "Tag 1.0.1" "$URL/branches/release-1.x" "$URL/tags/v1.0.1"
svn checkout -q "$URL/tags/v1.0.1" wc-tag
cd wc-tag
printf 'patched after tagging\n' > PATCHED.txt
svn add -q PATCHED.txt
svn commit -q -m "Oops: patch applied directly to the tag"
cd ..

# A branch that is deleted before HEAD.
svn copy -q -m "Short-lived experiment branch" "$URL/trunk" "$URL/branches/experiment"
svn delete -q -m "Abandon the experiment" "$URL/branches/experiment"

# More trunk history after the branching, so the graph is not trivial.
cd "$WC"
svn update -q
printf 'MIT License\n' > LICENSE
svn add -q LICENSE
svn commit -q -m "Add the licence file"
cd ..

# Spread the revisions across several authors, including one with no author at all.
LAST=$(svnlook youngest "$REPO")
i=1
while [ "$i" -le "$LAST" ]; do
  case $((i % 4)) in
    0) A="jsmith" ;;
    1) A="a.jones" ;;
    2) A="CONTOSO\\bwilliams" ;;
    3) A="" ;;
  esac
  if [ -n "$A" ]; then
    svn propset -q --revprop -r "$i" svn:author "$A" "$URL"
  else
    svn propdel -q --revprop -r "$i" svn:author "$URL" 2>/dev/null || true
  fi
  i=$((i + 1))
done

echo "fixture ready: $REPO (r$LAST)"
