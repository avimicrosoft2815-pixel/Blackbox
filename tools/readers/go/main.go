// bbx-read is an independent reader for BlackBox `.bbx` capsules, written in Go.
//
// It exists to answer a question one implementation cannot answer about itself: **is `docs/format.md` enough to read a
// capsule?** This file was written from `docs/format.md` §4 (frames), §5 (segments), §7 (payload encoding) and §8.1 (the
// capsule), plus the attacker table in `docs/BLACKBOX_FINAL_ARCHITECTURE_v1.1.md` §13.4 for the refusal codes, and it
// imports nothing outside Go's standard library (`archive/zip`, `crypto/sha256`, `encoding/hex`, `encoding/json`,
// `hash/crc32`, `os`). It does not read `src/blackbox/cap/format.py`; where the document did not say something the code
// needed, that is marked with `// DOC:` and the detail was taken from the conformance vectors or left out rather than
// guessed.
//
//	bbx-read <capsule.bbx> [--json]     verify one capsule
//	bbx-read --vectors <crc32.json>     reproduce the CRC-32 conformance vectors
//
// Exit codes mirror `blackbox verify` and the Node reader in `tools/readers/bbx-read.mjs`:
//
//	0  every check this reader implements passed
//	3  refused: a named code (BBX-E4xx) and no bytes trusted
//	4  an integrity claim did not hold (the file is a capsule, the claim is wrong)
//	2  usage
//
// What it checks, in order: the archive's name/limit/size rules before any entry is trusted (§8.1 "reading validates
// names … before it trusts a single entry"), every entry's sha256 against `INTEGRITY.sha256`, the root against
// `manifest.integrity.root`, the frame structure of every `events/seg-*.raw`, and the frame count against
// `manifest.counts.events`. What it does not check: `derived_parts` (re-rendering the JSONL/OTLP exports is the Python
// `capsule verify`'s job and needs the exporters), and any semantic claim in the manifest that is not a hash or a count.
package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"hash/crc32"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

const (
	maxEntries    = 5000
	maxTotalBytes = int64(4) << 30
	maxRatio      = 100
	segmentHeader = 4096
	frameHeader   = 48
	frameTrailer  = 8
)

// refusal is a named, coded rejection: the same shape `blackbox verify` reports.
type refusal struct {
	code string
	msg  string
}

func (r *refusal) Error() string { return r.code + ": " + r.msg }

func refuse(code, format string, args ...any) error {
	return &refusal{code: code, msg: fmt.Sprintf(format, args...)}
}

// integrityFailure is a claim that did not hold — a capsule whose own root disagrees with its bytes.
type integrityFailure struct {
	msg string
}

func (i *integrityFailure) Error() string { return i.msg }

type manifest struct {
	Format        string `json:"format"`
	FormatVersion int    `json:"format_version"`
	MinReader     string `json:"min_reader"`
	Integrity     struct {
		Alg     string `json:"alg"`
		Entries int    `json:"entries"`
		Root    string `json:"root"`
	} `json:"integrity"`
	Counts struct {
		Events int `json:"events"`
		Nodes  int `json:"nodes"`
	} `json:"counts"`
	Run struct {
		RunID        string `json:"run_id"`
		Status       string `json:"status"`
		StatusCause  string `json:"status_cause"`
		Completeness string `json:"completeness"`
	} `json:"run"`
}

type report struct {
	Path     string   `json:"path"`
	Bytes    int64    `json:"bytes"`
	Entries  int      `json:"entries"`
	Format   string   `json:"format"`
	Run      string   `json:"run"`
	Status   string   `json:"status"`
	Root     string   `json:"root"`
	Frames   int      `json:"frames"`
	Declared int      `json:"declared_events"`
	Problems []string `json:"problems"`
}

func main() {
	args := os.Args[1:]
	if len(args) == 0 {
		usage()
	}
	if args[0] == "--vectors" {
		if len(args) != 2 {
			usage()
		}
		os.Exit(runVectors(args[1]))
	}
	path := args[0]
	asJSON := false
	for _, arg := range args[1:] {
		switch arg {
		case "--json":
			asJSON = true
		case "--quiet":
			// accepted for parity with the Node reader: it prints nothing extra today
		default:
			usage()
		}
	}
	rep, err := verify(path)
	if err != nil {
		switch e := err.(type) {
		case *refusal:
			fmt.Fprintf(os.Stderr, "bbx-read: refused %s: %s (%s)\n", e.code, e.msg, path)
			os.Exit(3)
		case *integrityFailure:
			fmt.Fprintf(os.Stderr, "bbx-read: integrity failed: %s (%s)\n", e.msg, path)
			os.Exit(4)
		default:
			fmt.Fprintf(os.Stderr, "bbx-read: %v\n", err)
			os.Exit(3)
		}
	}
	if asJSON {
		blob, _ := json.MarshalIndent(rep, "", "  ")
		fmt.Println(string(blob))
	} else {
		fmt.Printf("bbx-read: verified %s (%d B, %d entries, run %s, status %s)\n", path, rep.Bytes, rep.Entries,
			rep.Run, rep.Status)
		fmt.Printf("  format %s, root %s, %d frames (manifest declares %d)\n", rep.Format, rep.Root, rep.Frames,
			rep.Declared)
	}
	os.Exit(0)
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: bbx-read <capsule.bbx> [--json] [--quiet] | bbx-read --vectors <crc32.json>")
	os.Exit(2)
}

// verify is the whole check: names and limits, entries, the integrity list, the root, then the frames.
func verify(path string) (report, error) {
	rep := report{Path: path, Problems: []string{}}
	info, err := os.Stat(path)
	if err != nil {
		return rep, refuse("BBX-E402", "cannot stat: %v", err)
	}
	rep.Bytes = info.Size()
	reader, err := zip.OpenReader(path)
	if err != nil {
		// DOC: §13.4 check 1 — a file that is not a ZIP is E402, not "unreadable".
		return rep, refuse("BBX-E402", "not a readable zip archive: %v", err)
	}
	defer reader.Close()
	return verifyArchive(reader, rep)
}

func verifyArchive(reader *zip.ReadCloser, rep report) (report, error) {
	files := reader.File
	rep.Entries = len(files)
	if len(files) > maxEntries {
		return rep, refuse("BBX-E402", "%d entries exceeds the %d-entry limit", len(files), maxEntries)
	}
	// ── names, limits and methods, before any entry is trusted (§8.1) ────────────────────────────────
	seen := map[string]string{}
	var total int64
	for _, file := range files {
		if err := checkName(file.Name); err != nil {
			return rep, err
		}
		folded := strings.ToLower(file.Name)
		if previous, ok := seen[folded]; ok {
			return rep, refuse("BBX-E404", "duplicate entry name (case-folded): %s and %s", file.Name, previous)
		}
		seen[folded] = file.Name
		total += int64(file.UncompressedSize64)
		if total > maxTotalBytes {
			return rep, refuse("BBX-E405", "declared total exceeds %d B", maxTotalBytes)
		}
		if file.CompressedSize64 > 0 && float64(file.UncompressedSize64)/float64(file.CompressedSize64) > maxRatio {
			return rep, refuse("BBX-E405", "%s expands more than %d:1", file.Name, maxRatio)
		}
		if file.Flags&0x1 != 0 {
			return rep, refuse("BBX-E406", "%s is encrypted", file.Name)
		}
		if file.Method != zip.Store && file.Method != zip.Deflate {
			return rep, refuse("BBX-E406", "%s uses compression method %d, not STORED or DEFLATE", file.Name,
				file.Method)
		}
	}
	// ── read every entry once, hashing as we go ──────────────────────────────────────────────────────
	contents := map[string][]byte{}
	digests := map[string]string{}
	for _, file := range files {
		data, err := readEntry(file, file.UncompressedSize64)
		if err != nil {
			return rep, err
		}
		contents[file.Name] = data
		sum := sha256.Sum256(data)
		digests[file.Name] = hex.EncodeToString(sum[:])
	}
	// ── the manifest ─────────────────────────────────────────────────────────────────────────────────
	rawManifest, ok := contents["manifest.json"]
	if !ok {
		return rep, refuse("BBX-E407", "no manifest.json")
	}
	var doc manifest
	if err := json.Unmarshal(rawManifest, &doc); err != nil {
		return rep, refuse("BBX-E407", "manifest.json is not JSON: %v", err)
	}
	if doc.Format != "bbx.capsule" {
		return rep, refuse("BBX-E407", "manifest.format is %q, not \"bbx.capsule\"", doc.Format)
	}
	if doc.FormatVersion > 0 {
		// DOC: §8.1 — a newer format is refused by version, never guessed at.
		return rep, refuse("BBX-E407", "manifest.format_version %d is newer than this reader's 0", doc.FormatVersion)
	}
	rep.Format = fmt.Sprintf("%s v%d", doc.Format, doc.FormatVersion)
	rep.Run = doc.Run.RunID
	rep.Status = doc.Run.Status
	rep.Declared = doc.Counts.Events
	if doc.Integrity.Entries != len(files) {
		// DOC: §8.1 — `integrity.entries` counts *all* archive members, the manifest and the list itself included.
		return rep, refuse("BBX-E407", "manifest.integrity.entries is %d, the archive holds %d members",
			doc.Integrity.Entries, len(files))
	}
	// ── INTEGRITY.sha256, then the root over it ──────────────────────────────────────────────────────
	listRaw, ok := contents["INTEGRITY.sha256"]
	if !ok {
		return rep, refuse("BBX-E407", "no INTEGRITY.sha256")
	}
	root := sha256.Sum256(listRaw)
	rep.Root = hex.EncodeToString(root[:])
	if doc.Integrity.Root != "" && doc.Integrity.Root != rep.Root {
		return rep, &integrityFailure{msg: fmt.Sprintf("manifest.integrity.root %s, but INTEGRITY.sha256 hashes to %s",
			doc.Integrity.Root, rep.Root)}
	}
	listed, err := parseIntegrityList(listRaw)
	if err != nil {
		return rep, err
	}
	expectedNames := make([]string, 0, len(contents)-2)
	for name := range contents {
		if name == "manifest.json" || name == "INTEGRITY.sha256" {
			continue
		}
		expectedNames = append(expectedNames, name)
	}
	sort.Strings(expectedNames)
	if len(listed) != len(expectedNames) {
		return rep, &integrityFailure{msg: fmt.Sprintf("INTEGRITY.sha256 lists %d entries, the archive holds %d",
			len(listed), len(expectedNames))}
	}
	for _, name := range expectedNames {
		want, ok := listed[name]
		if !ok {
			return rep, &integrityFailure{msg: "INTEGRITY.sha256 does not list " + name}
		}
		if digests[name] != want {
			return rep, &integrityFailure{msg: fmt.Sprintf("%s hashes to %s, the list says %s", name, digests[name], want)}
		}
	}
	// ── frames ───────────────────────────────────────────────────────────────────────────────────────
	frames := 0
	segments := make([]string, 0, len(contents))
	for name := range contents {
		if strings.HasPrefix(name, "events/seg-") && strings.HasSuffix(name, ".raw") {
			segments = append(segments, name)
		}
	}
	sort.Strings(segments)
	if len(segments) == 0 {
		rep.Problems = append(rep.Problems, "no segment entries: this capsule holds no frames")
	}
	for _, name := range segments {
		count, err := countFrames(contents[name], name)
		if err != nil {
			return rep, err
		}
		frames += count
	}
	rep.Frames = frames
	if doc.Counts.Events != 0 && frames != doc.Counts.Events {
		return rep, &integrityFailure{msg: fmt.Sprintf("manifest.counts.events is %d, the segments hold %d frames",
			doc.Counts.Events, frames)}
	}
	return rep, nil
}

// checkName refuses the names §13.4 names: absolute, traversal, drive letter, backslash, NUL and empty components.
func checkName(name string) error {
	if strings.ContainsRune(name, 0) {
		return refuse("BBX-E403", "entry name contains NUL: %q", name)
	}
	if strings.Contains(name, "\\") {
		return refuse("BBX-E403", "entry name contains a backslash: %q", name)
	}
	if strings.HasPrefix(name, "/") {
		return refuse("BBX-E403", "absolute entry name: %q", name)
	}
	if len(name) > 1 && name[1] == ':' {
		return refuse("BBX-E403", "drive-letter entry name: %q", name)
	}
	for _, part := range strings.Split(name, "/") {
		if part == ".." || part == "." || part == "" {
			return refuse("BBX-E403", "entry name escapes its root: %q", name)
		}
	}
	if name == "" {
		return refuse("BBX-E403", "empty entry name")
	}
	return nil
}

// readEntry reads one member in bounded chunks, refusing a header that lied about its size (§13.4 check 9).
func readEntry(file *zip.File, declared uint64) ([]byte, error) {
	rc, err := file.Open()
	if err != nil {
		return nil, refuse("BBX-E402", "%s: %v", file.Name, err)
	}
	defer rc.Close()
	limited := io.LimitReader(rc, int64(declared)+1)
	data, err := io.ReadAll(limited)
	if err != nil {
		return nil, refuse("BBX-E402", "%s: read failed (%v)", file.Name, err)
	}
	if uint64(len(data)) > declared {
		return nil, refuse("BBX-E409", "%s produced more than the %d B its header declares", file.Name, declared)
	}
	if uint64(len(data)) != declared {
		return nil, refuse("BBX-E402", "%s is %d B, declared %d B", file.Name, len(data), declared)
	}
	return data, nil
}

// parseIntegrityList reads `INTEGRITY.sha256`: 64 lower-case hex, two spaces, the entry name, LF, including after the
// last line. Anything else is a refusal, because a list a reader half-understands is not a list.
func parseIntegrityList(raw []byte) (map[string]string, error) {
	out := map[string]string{}
	text := string(raw)
	if text == "" {
		return out, nil
	}
	if !strings.HasSuffix(text, "\n") {
		return nil, refuse("BBX-E407", "INTEGRITY.sha256 does not end with a newline")
	}
	for _, line := range strings.Split(strings.TrimSuffix(text, "\n"), "\n") {
		if len(line) < 64+2+1 {
			return nil, refuse("BBX-E407", "INTEGRITY.sha256 line too short: %q", line)
		}
		sum := line[:64]
		if line[64:66] != "  " {
			return nil, refuse("BBX-E407", "INTEGRITY.sha256 line is not '<hex>  <name>': %q", line)
		}
		name := line[66:]
		if _, err := hex.DecodeString(sum); err != nil || strings.ToLower(sum) != sum {
			return nil, refuse("BBX-E407", "INTEGRITY.sha256 has a non-hex or upper-case digest: %q", line)
		}
		out[name] = sum
	}
	return out, nil
}

// countFrames walks one segment: the 4096 B header, then frames until the body ends.
func countFrames(blob []byte, name string) (int, error) {
	if len(blob) < segmentHeader {
		return 0, refuse("BBX-E402", "%s is %d B, shorter than a segment header", name, len(blob))
	}
	if !bytes.Equal(blob[:8], []byte("BBXSEG2\x00")) {
		return 0, refuse("BBX-E402", "%s has no BBXSEG2 magic", name)
	}
	// DOC: §5 — `header checksum` is CRC-32/ISO-HDLC over bytes [0, 96).
	want := order(blob[96:100])
	got := crc32.ChecksumIEEE(blob[:96])
	if got != want {
		return 0, &integrityFailure{msg: fmt.Sprintf("%s: segment header CRC-32 %08x, recorded %08x", name, got, want)}
	}
	off := segmentHeader
	frames := 0
	for off+frameHeader+frameTrailer <= len(blob) {
		if !bytes.Equal(blob[off:off+4], []byte("BBXF")) {
			break // zero padding at the end of a preallocated segment is not a frame
		}
		frameLen := int(order(blob[off+4 : off+8]))
		if frameLen < frameHeader+frameTrailer || off+frameLen > len(blob) {
			return 0, &integrityFailure{msg: fmt.Sprintf("%s: frame %d at %d has length %d, past the segment", name,
				frames, off, frameLen)}
		}
		payloadLen := int(order(blob[off+40 : off+44]))
		if frameHeader+payloadLen+frameTrailer > frameLen {
			return 0, &integrityFailure{msg: fmt.Sprintf("%s: frame %d declares %d payload bytes, frame is %d B",
				name, frames, payloadLen, frameLen)}
		}
		tailAt := off + frameLen - frameTrailer
		if !bytes.Equal(blob[tailAt:tailAt+4], []byte("BBXT")) {
			return 0, &integrityFailure{msg: fmt.Sprintf("%s: frame %d has no BBXT trailer", name, frames)}
		}
		if int(order(blob[tailAt+4:tailAt+8])) != frameLen {
			return 0, &integrityFailure{msg: fmt.Sprintf("%s: frame %d trailer says %d, header says %d", name, frames,
				order(blob[tailAt+4:tailAt+8]), frameLen)}
		}
		// DOC: §4 — the CRC covers `header[0,44) ‖ payload ‖ padding ‖ trailer`, i.e. every byte after the header's
		// 44th byte to the end of the frame. This is the detail defect 85 added to the document.
		recorded := order(blob[off+44 : off+48])
		hash := crc32.NewIEEE()
		hash.Write(blob[off : off+44])
		hash.Write(blob[off+frameHeader : off+frameLen])
		if hash.Sum32() != recorded {
			return 0, &integrityFailure{msg: fmt.Sprintf("%s: frame %d CRC-32 %08x, recorded %08x", name, frames,
				hash.Sum32(), recorded)}
		}
		frames++
		off += frameLen
	}
	return frames, nil
}

func order(b []byte) uint32 {
	switch len(b) {
	case 2:
		return uint32(b[0]) | uint32(b[1])<<8
	case 4:
		return uint32(b[0]) | uint32(b[1])<<8 | uint32(b[2])<<16 | uint32(b[3])<<24
	default:
		return 0
	}
}

// ── conformance vectors ──────────────────────────────────────────────────────────────────────────────

type vectorFile struct {
	Algorithm string `json:"algorithm"`
	Vectors   []struct {
		InputASCII string `json:"input_ascii"`
		InputHex   string `json:"input_hex"`
		CRCCode    string `json:"crc32"`
	} `json:"vectors"`
}

// runVectors reproduces `tests/conformance/vectors/crc32.json` and returns 0 only if every vector matched.
func runVectors(path string) int {
	raw, err := os.ReadFile(path)
	if err != nil {
		fmt.Fprintf(os.Stderr, "bbx-read: cannot read %s: %v\n", path, err)
		return 2
	}
	var doc vectorFile
	if err := json.Unmarshal(raw, &doc); err != nil {
		fmt.Fprintf(os.Stderr, "bbx-read: %s is not JSON: %v\n", path, err)
		return 2
	}
	failures := 0
	for index, vector := range doc.Vectors {
		var input []byte
		if vector.InputASCII != "" || vector.InputHex == "" {
			input = []byte(vector.InputASCII)
		} else {
			decoded, err := hex.DecodeString(vector.InputHex)
			if err != nil {
				fmt.Fprintf(os.Stderr, "bbx-read: vector %d has unreadable hex %q\n", index, vector.InputHex)
				failures++
				continue
			}
			input = decoded
		}
		got := fmt.Sprintf("0x%08X", crc32.ChecksumIEEE(input))
		if got != strings.ToUpper(vector.CRCCode) && got != vector.CRCCode {
			fmt.Fprintf(os.Stderr, "bbx-read: vector %d (%q) produced %s, document says %s\n", index, vector.InputASCII,
				got, vector.CRCCode)
			failures++
		}
	}
	if failures > 0 {
		fmt.Fprintf(os.Stderr, "bbx-read: %d of %d vectors failed\n", failures, len(doc.Vectors))
		return 3
	}
	fmt.Printf("bbx-read: %d CRC-32 vectors reproduced from %s\n", len(doc.Vectors), filepath.Base(path))
	return 0
}
