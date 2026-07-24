package main

import (
	"archive/tar"
	"bufio"
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// runPhase invokes run() as the CLI would, capturing the stdout trailer.
func runPhase(t *testing.T, args ...string) trailer {
	t.Helper()
	oldArgs, oldStdout := os.Args, os.Stdout
	defer func() { os.Args, os.Stdout = oldArgs, oldStdout }()

	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	os.Args = append([]string{"probe-sandbox-snapshot"}, args...)
	os.Stdout = w
	runErr := run()
	w.Close()
	out, _ := io.ReadAll(r)
	if runErr != nil {
		t.Fatalf("run(%v): %v", args, runErr)
	}

	var line string
	for _, candidate := range strings.Split(string(out), "\n") {
		if strings.HasPrefix(candidate, trailerPrefix) {
			line = strings.TrimPrefix(candidate, trailerPrefix)
		}
	}
	if line == "" {
		t.Fatalf("no trailer in output: %q", out)
	}
	var tr trailer
	if err := json.Unmarshal([]byte(line), &tr); err != nil {
		t.Fatalf("trailer parse: %v", err)
	}
	return tr
}

func readManifest(t *testing.T, path string) map[string]manifestEntry {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	entries := map[string]manifestEntry{}
	scanner := bufio.NewScanner(gz)
	scanner.Buffer(make([]byte, 1<<16), 1<<22)
	for scanner.Scan() {
		var e manifestEntry
		if err := json.Unmarshal(scanner.Bytes(), &e); err != nil {
			t.Fatalf("manifest line %q: %v", scanner.Text(), err)
		}
		entries[e.Path] = e
	}
	return entries
}

func fileSha256(t *testing.T, path string) string {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		t.Fatal(err)
	}
	return hex.EncodeToString(h.Sum(nil))
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func deltaMembers(t *testing.T, path string) map[string]string {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	tr := tar.NewReader(gz)
	members := map[string]string{}
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(tr)
		members[hdr.Name] = string(body)
	}
	return members
}

func TestBeginManifestAndTrailerIntegrity(t *testing.T) {
	root, work := t.TempDir(), t.TempDir()
	writeFile(t, filepath.Join(root, "workspace", "main.py"), "print('hi')\n")
	writeFile(t, filepath.Join(root, "hidden", ".secret"), "shh")
	if err := os.Symlink("workspace/main.py", filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}

	tr := runPhase(t, "begin", "--workdir", work, "--root", root)

	manifestPath := filepath.Join(work, "begin-manifest.jsonl.gz")
	entries := readManifest(t, manifestPath)
	if e, ok := entries[filepath.Join(root, "workspace", "main.py")]; !ok || e.Type != "f" || e.Size != 12 {
		t.Fatalf("main.py entry wrong: %+v ok=%v", e, ok)
	}
	if e, ok := entries[filepath.Join(root, "hidden", ".secret")]; !ok || e.Type != "f" {
		t.Fatalf("hidden file missing: %+v ok=%v", e, ok)
	}
	if e, ok := entries[filepath.Join(root, "link")]; !ok || e.Type != "l" || e.Link != "workspace/main.py" {
		t.Fatalf("symlink entry wrong: %+v ok=%v", e, ok)
	}
	got := tr.Files["begin-manifest.jsonl.gz"]
	if got.Sha256 != fileSha256(t, manifestPath) {
		t.Fatalf("trailer sha mismatch: %s", got.Sha256)
	}
	if tr.Truncated || len(tr.Errors) != 0 {
		t.Fatalf("unexpected truncation/errors: %+v", tr)
	}
}

func TestEndDeltaAddedModifiedDeleted(t *testing.T) {
	root, work1, work2 := t.TempDir(), t.TempDir(), t.TempDir()
	writeFile(t, filepath.Join(root, "keep.txt"), "same")
	writeFile(t, filepath.Join(root, "mod.txt"), "before")
	writeFile(t, filepath.Join(root, "gone.txt"), "bye")

	runPhase(t, "begin", "--workdir", work1, "--root", root)

	writeFile(t, filepath.Join(root, "mod.txt"), "after-longer")
	writeFile(t, filepath.Join(root, "new.txt"), "fresh")
	if err := os.Remove(filepath.Join(root, "gone.txt")); err != nil {
		t.Fatal(err)
	}

	tr := runPhase(t, "end", "--workdir", work2, "--root", root,
		"--begin-manifest", filepath.Join(work1, "begin-manifest.jsonl.gz"))

	if tr.Stats["added"] != 1 || tr.Stats["modified"] != 1 || tr.Stats["deleted"] != 1 {
		t.Fatalf("stats wrong: %+v", tr.Stats)
	}
	members := deltaMembers(t, filepath.Join(work2, "end-delta.tar.gz"))
	rel := func(p string) string { return strings.TrimPrefix(filepath.Join(root, p), "/") }
	if members[rel("new.txt")] != "fresh" {
		t.Fatalf("added file not in delta: %v", members)
	}
	if members[rel("mod.txt")] != "after-longer" {
		t.Fatalf("modified file not in delta: %v", members)
	}
	if _, ok := members[rel("keep.txt")]; ok {
		t.Fatalf("unchanged file leaked into delta: %v", members)
	}
	got := tr.Files["end-delta.tar.gz"]
	if got.Sha256 != fileSha256(t, filepath.Join(work2, "end-delta.tar.gz")) {
		t.Fatal("delta trailer sha mismatch")
	}
}

func TestHostileFilenamesSurviveJSON(t *testing.T) {
	root, work := t.TempDir(), t.TempDir()
	hostile := "evil\nname\twith\"quotes"
	writeFile(t, filepath.Join(root, hostile), "payload")

	runPhase(t, "begin", "--workdir", work, "--root", root)

	entries := readManifest(t, filepath.Join(work, "begin-manifest.jsonl.gz"))
	if e, ok := entries[filepath.Join(root, hostile)]; !ok || e.Size != 7 {
		t.Fatalf("hostile filename lost: %+v (have %d entries)", e, len(entries))
	}
}

func TestMaxFilesGuardTruncates(t *testing.T) {
	root, work := t.TempDir(), t.TempDir()
	for _, name := range []string{"a", "b", "c", "d", "e"} {
		writeFile(t, filepath.Join(root, name), name)
	}

	tr := runPhase(t, "begin", "--workdir", work, "--root", root, "--max-files", "3")
	if !tr.Truncated {
		t.Fatalf("expected truncation: %+v", tr)
	}
}

func TestDeltaBudgetDropsAndRecords(t *testing.T) {
	root, work1, work2 := t.TempDir(), t.TempDir(), t.TempDir()
	writeFile(t, filepath.Join(root, "seed.txt"), "s")
	runPhase(t, "begin", "--workdir", work1, "--root", root)

	writeFile(t, filepath.Join(root, "big.bin"), strings.Repeat("x", 4096))
	tr := runPhase(t, "end", "--workdir", work2, "--root", root,
		"--begin-manifest", filepath.Join(work1, "begin-manifest.jsonl.gz"),
		"--max-delta-bytes", "100")

	if !tr.Truncated || tr.DroppedCount != 1 {
		t.Fatalf("expected one dropped file: %+v", tr)
	}
	if len(tr.Dropped) != 1 || !strings.HasSuffix(tr.Dropped[0], "big.bin") {
		t.Fatalf("dropped path not named: %+v", tr.Dropped)
	}
}

func TestHashModeRecordsDigests(t *testing.T) {
	root, work := t.TempDir(), t.TempDir()
	writeFile(t, filepath.Join(root, "x.txt"), "content")

	tr := runPhase(t, "begin", "--workdir", work, "--root", root, "--hash")
	if tr.HashMode != "sha256" {
		t.Fatalf("hash mode not recorded: %+v", tr)
	}
	entries := readManifest(t, filepath.Join(work, "begin-manifest.jsonl.gz"))
	e := entries[filepath.Join(root, "x.txt")]
	if e.Hash != fileSha256(t, filepath.Join(root, "x.txt")) {
		t.Fatalf("hash wrong: %+v", e)
	}
}

func TestWorkdirAndExcludesSkipped(t *testing.T) {
	root := t.TempDir()
	work := filepath.Join(root, "work")
	writeFile(t, filepath.Join(root, "real.txt"), "keep me")
	writeFile(t, filepath.Join(root, "noise", "skip.txt"), "skip me")

	runPhase(t, "begin", "--workdir", work, "--root", root,
		"--exclude", filepath.Join(root, "noise"))

	entries := readManifest(t, filepath.Join(work, "begin-manifest.jsonl.gz"))
	for path := range entries {
		if strings.HasPrefix(path, work) {
			t.Fatalf("workdir leaked into manifest: %s", path)
		}
		if strings.Contains(path, "noise") {
			t.Fatalf("excluded path leaked: %s", path)
		}
	}
	if _, ok := entries[filepath.Join(root, "real.txt")]; !ok {
		t.Fatal("real file missing")
	}
}

func TestTypeChangeIsModified(t *testing.T) {
	root, work1, work2 := t.TempDir(), t.TempDir(), t.TempDir()
	target := filepath.Join(root, "thing")
	if err := os.Mkdir(target, 0o755); err != nil {
		t.Fatal(err)
	}
	runPhase(t, "begin", "--workdir", work1, "--root", root)

	if err := os.Remove(target); err != nil {
		t.Fatal(err)
	}
	writeFile(t, target, "now a file")
	tr := runPhase(t, "end", "--workdir", work2, "--root", root,
		"--begin-manifest", filepath.Join(work1, "begin-manifest.jsonl.gz"))
	if tr.Stats["modified"] != 1 {
		t.Fatalf("dir->file should count as modified: %+v", tr.Stats)
	}
}
