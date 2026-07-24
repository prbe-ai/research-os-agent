// probe-sandbox-snapshot: ephemeral begin/end filesystem snapshots for the
// probe.sandbox-state/1 contract (docs/2026-07-23-sandbox-state-capture.md).
//
// Runs inside an arbitrary sandbox image with zero dependencies (static
// binary, Go stdlib only). The sandbox filesystem is agent-authored and
// treated as adversarial input: paths are JSON-encoded, nothing is parsed
// from filenames, and every guard that drops data records what it dropped.
//
// Subcommands:
//
//	begin --workdir DIR                          -> DIR/begin-manifest.jsonl.gz
//	end   --workdir DIR --begin-manifest FILE    -> DIR/end-manifest.jsonl.gz + DIR/end-delta.tar.gz
//
// Both print a single trailer line to stdout, prefixed "PSBX1 ", carrying
// the sha256/size of every output file plus scan stats. The trailer is the
// bridge's integrity side-channel; it never round-trips through the
// container filesystem.
package main

import (
	"archive/tar"
	"bufio"
	"compress/gzip"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"hash"
	"hash/fnv"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"
)

const (
	trailerPrefix        = "PSBX1 "
	defaultMaxFiles      = 2_000_000
	defaultMaxDeltaBytes = int64(2) << 30 // 2 GiB, further capped by free space
	// Cap on how many dropped/errored paths we name individually; counts are
	// always exact.
	maxRecordedPaths = 100
)

type manifestEntry struct {
	Path    string  `json:"p"`
	PathB64 string  `json:"pb,omitempty"` // base64(raw path); set iff Path is not valid UTF-8
	Type    string  `json:"t"`
	Size    int64   `json:"s,omitempty"`
	Mtim    float64 `json:"m,omitempty"`
	Mode    string  `json:"mode,omitempty"`
	UID     int64   `json:"u"`
	GID     int64   `json:"g"`
	Link    string  `json:"lt,omitempty"`
	Hash    string  `json:"h,omitempty"`
}

type outputFile struct {
	Sha256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
}

type trailer struct {
	Schema        string                `json:"schema"`
	Phase         string                `json:"phase"`
	Files         map[string]outputFile `json:"files"`
	Stats         map[string]int64      `json:"stats"`
	SkippedMounts []string              `json:"skipped_mounts"`
	Truncated     bool                  `json:"truncated"`
	Dropped       []string              `json:"dropped"`
	DroppedCount  int64                 `json:"dropped_count"`
	Errors        []string              `json:"errors"`
	HashMode      string                `json:"hash_mode"`
}

type scanConfig struct {
	root         string
	workdir      string
	exclude      []string
	hashFiles    bool
	maxFiles     int64
	oneFS        bool
	deadline     time.Time // zero = no self-imposed deadline
	rootDev      uint64
	haveRootDev  bool
	skippedMnts  []string
	errs         []string
	filesScanned int64
	entries      int64
	truncated    bool
	deadlineHit  bool
}

func (c *scanConfig) recordErr(context string, err error) {
	if len(c.errs) < maxRecordedPaths {
		c.errs = append(c.errs, fmt.Sprintf("%s: %v", context, err))
	}
}

func (c *scanConfig) excluded(path string) bool {
	for _, prefix := range c.exclude {
		if path == prefix || strings.HasPrefix(path, prefix+"/") {
			return true
		}
	}
	return false
}

// statDevice returns the device id for cross-mount detection.
func statDevice(info fs.FileInfo) (uint64, bool) {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, false
	}
	return uint64(st.Dev), true //nolint:unconvert // Dev width differs per GOOS
}

func statOwner(info fs.FileInfo) (uid, gid int64, mode string, mtime float64) {
	mtime = float64(info.ModTime().UnixNano()) / 1e9
	mode = fmt.Sprintf("%06o", info.Mode().Perm()|fileTypeBits(info.Mode()))
	if st, ok := info.Sys().(*syscall.Stat_t); ok {
		uid = int64(st.Uid)
		gid = int64(st.Gid)
	}
	return uid, gid, mode, mtime
}

func fileTypeBits(m fs.FileMode) fs.FileMode {
	switch {
	case m.IsRegular():
		return 0o100000
	case m.IsDir():
		return 0o040000
	case m&fs.ModeSymlink != 0:
		return 0o120000
	default:
		return 0
	}
}

func entryType(m fs.FileMode) string {
	switch {
	case m.IsRegular():
		return "f"
	case m.IsDir():
		return "d"
	case m&fs.ModeSymlink != 0:
		return "l"
	case m&fs.ModeSocket != 0:
		return "s"
	case m&fs.ModeNamedPipe != 0:
		return "p"
	case m&fs.ModeDevice != 0:
		return "b"
	default:
		return "?"
	}
}

func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// walk streams manifest entries in directory-walk order (host sorts later —
// keeps in-container memory flat). visit is called for every inventoried
// entry; returning an error aborts the walk.
func (c *scanConfig) walk(visit func(manifestEntry, fs.FileInfo) error) error {
	rootInfo, err := os.Lstat(c.root)
	if err != nil {
		return fmt.Errorf("stat root: %w", err)
	}
	if dev, ok := statDevice(rootInfo); ok {
		c.rootDev, c.haveRootDev = dev, true
	}

	return filepath.WalkDir(c.root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			c.recordErr(path, err)
			if d != nil && d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if path != c.root && c.excluded(path) {
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		info, lerr := d.Info()
		if lerr != nil {
			c.recordErr(path, lerr)
			return nil
		}
		if d.IsDir() && path != c.root && c.oneFS && c.haveRootDev {
			if dev, ok := statDevice(info); ok && dev != c.rootDev {
				if len(c.skippedMnts) < maxRecordedPaths {
					c.skippedMnts = append(c.skippedMnts, path)
				}
				return filepath.SkipDir
			}
		}
		c.entries++
		if c.entries > c.maxFiles {
			c.truncated = true
			return fmt.Errorf("max_files guard reached (%d)", c.maxFiles)
		}
		// Self-deadline: bound in-container wall-clock so a pathological tree
		// (or a symlink loop the walker somehow follows) can't outlive the
		// bridge's exec timeout and leave a process running past the agent
		// phase. Checked every 4096 entries to keep time.Now() off the hot path.
		if !c.deadline.IsZero() && c.entries%4096 == 0 && time.Now().After(c.deadline) {
			c.truncated = true
			c.deadlineHit = true
			return fmt.Errorf("scan deadline reached after %d entries", c.entries)
		}
		entry := manifestEntry{Path: path, Type: entryType(info.Mode())}
		if !utf8.ValidString(path) {
			entry.PathB64 = base64.StdEncoding.EncodeToString([]byte(path))
		}
		uid, gid, mode, mtime := statOwner(info)
		entry.UID, entry.GID, entry.Mode, entry.Mtim = uid, gid, mode, mtime
		if info.Mode().IsRegular() {
			c.filesScanned++
			entry.Size = info.Size()
			if c.hashFiles {
				if h, herr := hashFile(path); herr == nil {
					entry.Hash = h
				} else {
					c.recordErr(path, herr)
				}
			}
		}
		if info.Mode()&fs.ModeSymlink != 0 {
			if target, terr := os.Readlink(path); terr == nil {
				entry.Link = target
			} else {
				c.recordErr(path, terr)
			}
		}
		return visit(entry, info)
	})
}

// countingHashWriter tees writes into sha256 + size so output integrity is
// known without re-reading the file.
type countingHashWriter struct {
	w io.Writer
	h hash.Hash
	n int64
}

func newCountingHashWriter(w io.Writer) *countingHashWriter {
	return &countingHashWriter{w: w, h: sha256.New()}
}

func (c *countingHashWriter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.h.Write(p[:n])
	c.n += int64(n)
	return n, err
}

func (c *countingHashWriter) sum() outputFile {
	return outputFile{Sha256: hex.EncodeToString(c.h.Sum(nil)), SizeBytes: c.n}
}

type manifestWriter struct {
	file *os.File
	chw  *countingHashWriter
	gz   *gzip.Writer
	bw   *bufio.Writer
	enc  *json.Encoder
}

func newManifestWriter(path string) (*manifestWriter, error) {
	f, err := os.Create(path)
	if err != nil {
		return nil, err
	}
	chw := newCountingHashWriter(f)
	gz := gzip.NewWriter(chw)
	bw := bufio.NewWriterSize(gz, 1<<16)
	return &manifestWriter{file: f, chw: chw, gz: gz, bw: bw, enc: json.NewEncoder(bw)}, nil
}

func (m *manifestWriter) write(e manifestEntry) error { return m.enc.Encode(e) }

func (m *manifestWriter) close() (outputFile, error) {
	if err := m.bw.Flush(); err != nil {
		return outputFile{}, err
	}
	if err := m.gz.Close(); err != nil {
		return outputFile{}, err
	}
	if err := m.file.Sync(); err != nil {
		return outputFile{}, err
	}
	if err := m.file.Close(); err != nil {
		return outputFile{}, err
	}
	return m.chw.sum(), nil
}

// beginEntry is what the end phase diffs each walked path against. Storing the
// type letter and symlink target (not just size/mtime) lets classify catch a
// file<->symlink/dir retype and a symlink retarget, which size/mtime alone miss.
type beginEntry struct {
	typ  string
	size int64
	mtim float64
	link string
}

// beginIndex is the compact lookup the end phase diffs against, keyed by
// fnv64a of the RAW path bytes. A 64-bit collision (~2e-7 at 2M paths) would
// mask one modification — accepted and documented in the plan.
type beginIndex struct {
	entries map[uint64]beginEntry
	matched map[uint64]bool
}

// rawPath reconstructs the exact on-disk bytes of an entry: JSON marshaling
// mangles invalid-UTF-8 paths to U+FFFD, so those carry a base64 `pb` field
// that round-trips losslessly. Without this the begin/end keys diverge and
// untouched non-UTF-8-named files show as spurious added+deleted churn.
func rawPath(e manifestEntry) string {
	if e.PathB64 != "" {
		if decoded, err := base64.StdEncoding.DecodeString(e.PathB64); err == nil {
			return string(decoded)
		}
	}
	return e.Path
}

func pathKey(p string) uint64 {
	h := fnv.New64a()
	h.Write([]byte(p))
	return h.Sum64()
}

func loadBeginIndex(path string) (*beginIndex, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return nil, err
	}
	defer gz.Close()
	idx := &beginIndex{entries: map[uint64]beginEntry{}, matched: map[uint64]bool{}}
	scanner := bufio.NewScanner(gz)
	scanner.Buffer(make([]byte, 1<<16), 1<<22) // paths can be ~4 MB of hostility
	for scanner.Scan() {
		var e manifestEntry
		if err := json.Unmarshal(scanner.Bytes(), &e); err != nil {
			return nil, fmt.Errorf("begin manifest line: %w", err)
		}
		idx.entries[pathKey(rawPath(e))] = beginEntry{
			typ: e.Type, size: e.Size, mtim: e.Mtim, link: e.Link,
		}
	}
	return idx, scanner.Err()
}

// classify returns "added", "modified", or "" (unchanged) for a walked entry.
func (b *beginIndex) classify(e manifestEntry) string {
	key := pathKey(rawPath(e))
	prev, ok := b.entries[key]
	if !ok {
		return "added"
	}
	b.matched[key] = true
	if prev.typ != e.Type {
		return "modified" // file<->symlink<->dir<->socket retype at the same path
	}
	switch e.Type {
	case "f":
		if prev.size != e.Size || prev.mtim != e.Mtim {
			return "modified"
		}
	case "l":
		if prev.link != e.Link {
			return "modified" // symlink retargeted in place
		}
	}
	return ""
}

func (b *beginIndex) deletedCount() int64 {
	return int64(len(b.entries) - len(b.matched))
}

type deltaWriter struct {
	file      *os.File
	chw       *countingHashWriter
	gz        *gzip.Writer
	tw        *tar.Writer
	budget    int64
	written   int64
	truncated bool
	dropped   []string
	droppedN  int64
}

func newDeltaWriter(path string, budget int64) (*deltaWriter, error) {
	f, err := os.Create(path)
	if err != nil {
		return nil, err
	}
	chw := newCountingHashWriter(f)
	gz := gzip.NewWriter(chw)
	return &deltaWriter{file: f, chw: chw, gz: gz, tw: tar.NewWriter(gz), budget: budget}, nil
}

func (d *deltaWriter) drop(path string) {
	d.truncated = true
	d.droppedN++
	if len(d.dropped) < maxRecordedPaths {
		d.dropped = append(d.dropped, path)
	}
}

func (d *deltaWriter) add(e manifestEntry, info fs.FileInfo) error {
	if e.Type == "l" {
		// Symlink headers are tiny but unbounded in count; charge an estimate
		// against the budget so a fork bomb of symlinks can't blow past the cap.
		cost := int64(len(e.Path) + len(e.Link) + 512)
		if d.written+cost > d.budget {
			d.drop(e.Path)
			return nil
		}
		hdr := &tar.Header{
			Name:     strings.TrimPrefix(e.Path, "/"),
			Typeflag: tar.TypeSymlink,
			Linkname: e.Link,
			Mode:     0o777,
			ModTime:  info.ModTime(),
		}
		if err := d.tw.WriteHeader(hdr); err != nil {
			return err
		}
		d.written += cost
		return nil
	}
	if e.Type != "f" {
		return nil
	}
	if d.written+e.Size > d.budget {
		d.drop(e.Path)
		return nil
	}
	f, err := os.Open(e.Path)
	if err != nil {
		return err
	}
	defer f.Close()
	hdr, err := tar.FileInfoHeader(info, "")
	if err != nil {
		return err
	}
	hdr.Name = strings.TrimPrefix(e.Path, "/")
	hdr.Uid = int(e.UID)
	hdr.Gid = int(e.GID)
	if err := d.tw.WriteHeader(hdr); err != nil {
		return err
	}
	// The file can shrink/grow between stat and read (agent daemons); tar
	// requires exactly hdr.Size bytes. Cut a grown file (CopyN caps at
	// hdr.Size) and zero-pad a shrunk one from a fixed buffer — never a single
	// hdr.Size-wide allocation, which an agent could inflate to the budget.
	n, err := io.CopyN(d.tw, f, hdr.Size)
	if err != nil && err != io.EOF {
		return err
	}
	if n < hdr.Size {
		if _, werr := io.CopyN(d.tw, zeroReader{}, hdr.Size-n); werr != nil {
			return werr
		}
	}
	d.written += e.Size
	return nil
}

// zeroReader is an infinite source of zero bytes, used to pad a file that
// shrank between stat and read without allocating the gap up front.
type zeroReader struct{}

func (zeroReader) Read(p []byte) (int, error) {
	for i := range p {
		p[i] = 0
	}
	return len(p), nil
}

func (d *deltaWriter) close() (outputFile, error) {
	if err := d.tw.Close(); err != nil {
		return outputFile{}, err
	}
	if err := d.gz.Close(); err != nil {
		return outputFile{}, err
	}
	if err := d.file.Sync(); err != nil {
		return outputFile{}, err
	}
	if err := d.file.Close(); err != nil {
		return outputFile{}, err
	}
	return d.chw.sum(), nil
}

func freeSpaceBudget(workdir string, maxDelta int64) int64 {
	var st syscall.Statfs_t
	if err := syscall.Statfs(workdir, &st); err != nil {
		return maxDelta
	}
	free := int64(st.Bavail) * int64(st.Bsize) //nolint:unconvert // field widths differ per GOOS
	if half := free / 2; half < maxDelta {
		return half
	}
	return maxDelta
}

func emitTrailer(t trailer) error {
	data, err := json.Marshal(t)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(os.Stdout, trailerPrefix+string(data))
	return err
}

func run() error {
	if len(os.Args) < 2 {
		return fmt.Errorf("usage: %s begin|end --workdir DIR [flags]", os.Args[0])
	}
	phase := os.Args[1]
	if phase != "begin" && phase != "end" {
		return fmt.Errorf("unknown phase %q (want begin|end)", phase)
	}

	flags := flag.NewFlagSet(phase, flag.ContinueOnError)
	workdir := flags.String("workdir", "", "output directory (required)")
	beginManifest := flags.String("begin-manifest", "", "begin manifest path (end phase)")
	root := flags.String("root", "/", "scan root")
	excludeFlag := flags.String("exclude", "", "colon-separated extra path prefixes to exclude")
	hashMode := flags.Bool("hash", false, "sha256 every regular file (slow, closes mtime-preserving edits)")
	maxFiles := flags.Int64("max-files", defaultMaxFiles, "inventory guard")
	maxDelta := flags.Int64("max-delta-bytes", defaultMaxDeltaBytes, "delta tar guard (further capped at 50% free space)")
	maxSeconds := flags.Float64("max-seconds", 0, "self-imposed wall-clock deadline; 0 disables. Set below the caller's exec timeout so the process exits itself.")
	if err := flags.Parse(os.Args[2:]); err != nil {
		return err
	}
	if *workdir == "" {
		return fmt.Errorf("--workdir is required")
	}
	if phase == "end" && *beginManifest == "" {
		return fmt.Errorf("end phase requires --begin-manifest")
	}
	if err := os.MkdirAll(*workdir, 0o700); err != nil {
		return err
	}

	exclude := []string{"/proc", "/sys", "/dev", "/logs", filepath.Clean(*workdir)}
	for _, extra := range strings.Split(*excludeFlag, ":") {
		if extra = strings.TrimSpace(extra); extra != "" {
			exclude = append(exclude, filepath.Clean(extra))
		}
	}

	cfg := &scanConfig{
		root:      filepath.Clean(*root),
		workdir:   filepath.Clean(*workdir),
		exclude:   exclude,
		hashFiles: *hashMode,
		maxFiles:  *maxFiles,
		oneFS:     true,
	}
	if *maxSeconds > 0 {
		cfg.deadline = time.Now().Add(time.Duration(*maxSeconds * float64(time.Second)))
	}

	out := trailer{
		Schema:   "probe.sandbox-snapshot-trailer/1",
		Phase:    phase,
		Files:    map[string]outputFile{},
		Stats:    map[string]int64{},
		HashMode: "fast",
	}
	if *hashMode {
		out.HashMode = "sha256"
	}

	manifestName := "begin-manifest.jsonl.gz"
	if phase == "end" {
		manifestName = "end-manifest.jsonl.gz"
	}
	mw, err := newManifestWriter(filepath.Join(*workdir, manifestName))
	if err != nil {
		return err
	}

	var idx *beginIndex
	var dw *deltaWriter
	var added, modified int64
	if phase == "end" {
		if idx, err = loadBeginIndex(*beginManifest); err != nil {
			return fmt.Errorf("load begin manifest: %w", err)
		}
		budget := freeSpaceBudget(*workdir, *maxDelta)
		if dw, err = newDeltaWriter(filepath.Join(*workdir, "end-delta.tar.gz"), budget); err != nil {
			return err
		}
		out.Stats["delta_budget_bytes"] = budget
	}

	walkErr := cfg.walk(func(e manifestEntry, info fs.FileInfo) error {
		if err := mw.write(e); err != nil {
			return err
		}
		if idx == nil {
			return nil
		}
		change := idx.classify(e)
		switch change {
		case "added":
			added++
		case "modified":
			modified++
		default:
			return nil
		}
		if e.Type == "f" || e.Type == "l" {
			if err := dw.add(e, info); err != nil {
				cfg.recordErr("delta "+e.Path, err)
			}
		}
		return nil
	})
	if walkErr != nil && !cfg.truncated {
		return walkErr
	}

	sum, err := mw.close()
	if err != nil {
		return err
	}
	out.Files[manifestName] = sum

	if dw != nil {
		deltaSum, err := dw.close()
		if err != nil {
			return err
		}
		out.Files["end-delta.tar.gz"] = deltaSum
		out.Truncated = cfg.truncated || dw.truncated
		out.Dropped = dw.dropped
		out.DroppedCount = dw.droppedN
		out.Stats["added"] = added
		out.Stats["modified"] = modified
		out.Stats["deleted"] = idx.deletedCount()
		out.Stats["begin_entries"] = int64(len(idx.entries))
	} else {
		out.Truncated = cfg.truncated
	}
	out.Stats["entries"] = cfg.entries
	out.Stats["files_scanned"] = cfg.filesScanned
	out.SkippedMounts = cfg.skippedMnts
	out.Errors = cfg.errs
	if cfg.deadlineHit {
		out.Errors = append(out.Errors, fmt.Sprintf("scan hit --max-seconds deadline after %d entries; manifest is partial", cfg.entries))
	}
	sort.Strings(out.SkippedMounts)

	return emitTrailer(out)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "probe-sandbox-snapshot: %v\n", err)
		os.Exit(1)
	}
}
