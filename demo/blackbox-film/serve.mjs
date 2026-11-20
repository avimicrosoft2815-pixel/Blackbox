#!/usr/bin/env node
/**
 * A small static server for the film and the studio, with two conveniences:
 *
 *   /                     → the Director Studio
 *   /film.mp4             → the 1080p film, sent as a download (this is the link to paste into a browser)
 *   /film-4k.mp4          → the 4K film, same way
 *   /export/*.mp4         → the same files, playable inline (byte ranges supported, so seeking works)
 *
 * It binds 0.0.0.0 so a preview proxy can reach it, and it does not care which Host header arrives. No directory
 * traversal outside the project, no uploads, no writes: this serves files and nothing else.
 */
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = resolve(fileURLToPath(new URL(".", import.meta.url)));
const port = Number(process.env.PORT ?? 8181);
const host = process.env.HOST ?? "0.0.0.0";

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".mp4": "video/mp4",
  ".webm": "video/webm",
  ".mp3": "audio/mpeg",
  ".srt": "application/x-subrip; charset=utf-8",
  ".vtt": "text/vtt; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".md": "text/markdown; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
};

const DOWNLOADS = new Map([
  ["/film.mp4", { file: "export/blackbox-film-1080p.mp4", name: "blackbox-film-1080p.mp4" }],
  ["/film-4k.mp4", { file: "export/blackbox-film-4k.mp4", name: "blackbox-film-4k.mp4" }],
]);

function safePath(urlPath) {
  const decoded = decodeURIComponent(urlPath.split("?")[0]);
  const target = normalize(join(projectRoot, decoded));
  if (!target.startsWith(projectRoot + sep) && target !== projectRoot) return null;
  return target;
}

function send(res, status, body, type = "text/plain; charset=utf-8") {
  res.writeHead(status, { "content-type": type, "cache-control": "no-store" });
  res.end(body);
}

async function serveFile(req, res, path, { download = null } = {}) {
  let info;
  try {
    info = await stat(path);
  } catch {
    return send(res, 404, "not found");
  }
  if (info.isDirectory()) return serveFile(req, res, join(path, "index.html"), { download });

  const type = TYPES[extname(path).toLowerCase()] ?? "application/octet-stream";
  const headers = {
    "content-type": type,
    "cache-control": "no-store",
    "accept-ranges": "bytes",
  };
  if (download) headers["content-disposition"] = `attachment; filename="${download}"`;

  const range = req.headers.range;
  if (range && /^bytes=/.test(range)) {
    const [startRaw, endRaw] = range.replace("bytes=", "").split("-");
    const start = Number.parseInt(startRaw, 10) || 0;
    const end = endRaw ? Math.min(Number.parseInt(endRaw, 10), info.size - 1) : info.size - 1;
    if (start >= info.size || end < start) {
      res.writeHead(416, { "content-range": `bytes */${info.size}` });
      return res.end();
    }
    res.writeHead(206, {
      ...headers,
      "content-range": `bytes ${start}-${end}/${info.size}`,
      "content-length": String(end - start + 1),
    });
    return createReadStream(path, { start, end }).pipe(res);
  }

  res.writeHead(200, { ...headers, "content-length": String(info.size) });
  if (req.method === "HEAD") return res.end();
  return createReadStream(path).pipe(res);
}

const server = createServer(async (req, res) => {
  if (req.method !== "GET" && req.method !== "HEAD") {
    return send(res, 405, "only GET and HEAD");
  }

  const pathname = req.url === "/" ? "/studio/index.html" : req.url;

  if (DOWNLOADS.has(pathname.split("?")[0])) {
    const entry = DOWNLOADS.get(pathname.split("?")[0]);
    return serveFile(req, res, join(projectRoot, entry.file), { download: entry.name });
  }

  // A bare /studio is a directory; /studio/ finds its index.
  const target = safePath(pathname);
  if (!target) return send(res, 403, "outside the project");
  return serveFile(req, res, target);
});

server.listen(port, host, () => {
  console.log(`BlackBox film server on http://${host}:${port}`);
  console.log(`  studio   http://localhost:${port}/studio/`);
  console.log(`  download http://localhost:${port}/film.mp4`);
});
