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
	Path string  `json:"p"`
	Type string  `json:"t"`
	Size int64   `json:"s,omitempty"`
	Mtim float64 `json:"m,omitempty"`
	Mode string  `json:"mode,omitempty"`
	UID  int64   `json:"u"`
	GID  int64   `json:"g"`
	Link string  `json:"lt,omitempty"`
	Hash string  `json:"h,omitempty"`
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
	rootDev      uint64
	haveRootDev  bool
	skippedMnts  []string
	errs         []string
	filesScanned int64
	entries      int64
	truncated    bool
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
		entry := manifestEntry{Path: path, Type: entryType(info.Mode())}
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

// beginIndex is the compact lookup the end phase diffs against: fnv64a(path)
// -> (size, mtime). ~32 B/entry keeps 2M files near 100 MB. A 64-bit
// collision (~2e-7 at 2M paths) would mask one modification — accepted and
// documented in the plan.
type beginIndex struct {
	entries map[uint64][2]float64 // [size, mtime]; dirs/symlinks store size -1
	matched map[uint64]bool
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
	idx := &beginIndex{entries: map[uint64][2]float64{}, matched: map[uint64]bool{}}
	scanner := bufio.NewScanner(gz)
	scanner.Buffer(make([]byte, 1<<16), 1<<22) // paths can be ~4 MB of hostility
	for scanner.Scan() {
		var e manifestEntry
		if err := json.Unmarshal(scanner.Bytes(), &e); err != nil {
			return nil, fmt.Errorf("begin manifest line: %w", err)
		}
		size := float64(e.Size)
		if e.Type != "f" {
			size = -1
		}
		idx.entries[pathKey(e.Path)] = [2]float64{size, e.Mtim}
	}
	return idx, scanner.Err()
}

// classify returns "added", "modified", or "" (unchanged) for a walked entry.
func (b *beginIndex) classify(e manifestEntry) string {
	key := pathKey(e.Path)
	prev, ok := b.entries[key]
	if !ok {
		return "added"
	}
	b.matched[key] = true
	if e.Type != "f" {
		return "" // non-regular: presence match is enough; bytes never archived
	}
	if prev[0] != float64(e.Size) || prev[1] != e.Mtim {
		return "modified"
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
		hdr := &tar.Header{
			Name:     strings.TrimPrefix(e.Path, "/"),
			Typeflag: tar.TypeSymlink,
			Linkname: e.Link,
			Mode:     0o777,
			ModTime:  info.ModTime(),
		}
		return d.tw.WriteHeader(hdr)
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
	// requires exactly hdr.Size bytes, so pad or cut to the declared size.
	n, err := io.CopyN(d.tw, f, hdr.Size)
	if n < hdr.Size {
		if _, werr := d.tw.Write(make([]byte, hdr.Size-n)); werr != nil {
			return werr
		}
	}
	if err != nil && err != io.EOF {
		return err
	}
	d.written += e.Size
	return nil
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
	sort.Strings(out.SkippedMounts)

	return emitTrailer(out)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintf(os.Stderr, "probe-sandbox-snapshot: %v\n", err)
		os.Exit(1)
	}
}
