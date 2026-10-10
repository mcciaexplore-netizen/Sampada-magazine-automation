import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { CheckSquare, FileText, FolderOpen, Headphones, LoaderCircle, Play, QrCode, Sheet, Sparkles, Square, Upload, Video } from "lucide-react";
import "./styles.css";
import mcciaLogo from "./assets/mccia-logo.png";

function CountUp({ value }) {
  const ref = useRef(null);
  const [shown, setShown] = useState(0);
  const done = useRef(false);
  const latest = useRef(value);
  latest.current = value;
  useEffect(() => {
    const el = ref.current;
    if (!el || done.current) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const io = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return;
      io.disconnect();
      if (reduce) { done.current = true; setShown(latest.current); return; }
      const start = performance.now();
      const tick = (now) => {
        const p = Math.min((now - start) / 1800, 1);
        setShown(Math.round(latest.current * (1 - Math.pow(1 - p, 3))));
        if (p < 1) requestAnimationFrame(tick); else done.current = true;
      };
      requestAnimationFrame(tick);
    }, { threshold: 0.1 });
    io.observe(el);
    return () => io.disconnect();
  }, []);
  return <span ref={ref}>{(done.current ? value : shown).toLocaleString("en-IN")}</span>;
}

const months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/+$/, "");

async function api(path, options = {}) {
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  const url = path.startsWith("http") ? path : `${API_BASE}${cleanPath}`;
  try {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.message || `Request failed (${response.status})`);
    return data;
  } catch (err) {
    if (err.message === "Failed to fetch" || err.name === "TypeError") {
      throw new Error("Cannot reach the Sampada backend. Start it (uvicorn backend_api:app) or, on Render, wait 30-45 seconds for it to wake up, then retry.");
    }
    throw err;
  }
}

// XMLHttpRequest instead of fetch so the browser can report upload progress.
function uploadWithProgress(path, body, onProgress) {
  const url = `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress({ phase: "upload", loaded: event.loaded, total: event.total }); };
    xhr.upload.onload = () => onProgress({ phase: "analyze" });
    xhr.onerror = () => reject(new Error("Cannot reach the Sampada backend. Start it (uvicorn backend_api:app) or, on Render, wait 30-45 seconds for it to wake up, then retry."));
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText); } catch { /* non-JSON error page */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.detail || data.message || `Request failed (${xhr.status})`));
    };
    xhr.send(body);
  });
}

const mb = (bytes) => (bytes / 1048576).toFixed(1);

function App() {
  const [year, setYear] = useState(2026);
  const [month, setMonth] = useState(9);
  const [driveRoot, setDriveRoot] = useState("Sampada Drive");
  const [file, setFile] = useState(null);
  const [issueKey, setIssueKey] = useState("");
  const [rows, setRows] = useState([]);
  const [links, setLinks] = useState([]);
  const [stats, setStats] = useState({ selected: 0, audio: 0, videos: 0, qr: 0 });
  const [busy, setBusy] = useState("");
  const [progress, setProgress] = useState(null);
  const [notice, setNotice] = useState(null);
  const [scriptsReady, setScriptsReady] = useState(false);
  const [folder, setFolder] = useState("");
  const [excelFile, setExcelFile] = useState("");
  const [google, setGoogle] = useState({ configured: null, help: "" });
  const [googleLinks, setGoogleLinks] = useState({ sheet_url: "", drive_folder_url: "" });

  useEffect(() => {
    let stop = false, timer;
    const check = () => api("/api/google/status").then(data => { if (!stop) setGoogle(data); }).catch(() => { if (!stop) timer = setTimeout(check, 3000); });
    check();
    return () => { stop = true; clearTimeout(timer); };
  }, []);
  const keepGoogleLinks = (data) => { if (data.sheet_url) setGoogleLinks({ sheet_url: data.sheet_url, drive_folder_url: data.drive_folder_url }); };

  const selectedCount = useMemo(() => rows.filter(row => String(row.selected).toLowerCase() === "yes" || row.selected === true).length, [rows]);
  const allAudio = stats.selected > 0 && stats.audio >= stats.selected;

  const withLiveStats = async (work) => {
    const refresh = async () => {
      try { const progress = await api(`/api/status/${issueKey}`); setStats(progress.stats); }
      catch { /* Source of truth */ }
    };
    const timer = window.setInterval(refresh, 1500);
    try { return await work(); }
    finally { window.clearInterval(timer); await refresh(); }
  };

  const run = async (label, work) => {
    setBusy(label); setNotice(null);
    try { await work(); }
    catch (error) { setNotice({ type: "error", text: error.message }); }
    finally { setBusy(""); }
  };

  const analyze = () => run("Analyzing magazine pages and detecting table of contents…", async () => {
    if (!file) throw new Error("Choose a Sampada PDF first");
    const body = new FormData();
    body.append("file", file); body.append("year", year); body.append("month", month); body.append("drive_root", driveRoot);
    setProgress({ phase: "upload", loaded: 0, total: file.size });
    let data;
    try { data = await uploadWithProgress("/api/analyze", body, setProgress); }
    finally { setProgress(null); }
    setIssueKey(data.issue_key); setRows(data.rows); setStats(data.stats); setScriptsReady(false); setFolder(data.monthly_folder);
    setGoogleLinks({ sheet_url: "", drive_folder_url: "" });
    setNotice({ type: data.fallback ? "warning" : "success", text: data.fallback ? "No 'Scan the QR code to listen…' box or QR symbol was found in this PDF. Tick the articles you want manually." : `Found ${data.stats.selected} articles that end with a QR code.` });
  });

  const runFullAutomation = () => run("Running full end-to-end automation (narration, 1080p videos, SRT captions, Excel plan, Google Drive upload & Google Sheet)…", async () => {
    if (selectedCount === 0) {
      throw new Error("Please select at least one article checkbox before running automation.");
    }
    await withLiveStats(async () => {
      const data = await api("/api/automate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ issue_key: issueKey, rows })
      });
      setStats(data.stats);
      setScriptsReady(true);
      setExcelFile(data.xlsx || "");
      keepGoogleLinks(data);
      setNotice({ type: "success", text: data.message });
    });
  });

  const saveReview = () => run("Creating Excel workbook and narration scripts with 8-9 YouTube hashtags…", async () => {
    if (selectedCount === 0) {
      throw new Error("Please select at least one article checkbox before proceeding.");
    }
    const data = await api("/api/review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issue_key: issueKey, rows }) });
    setStats(data.stats); setScriptsReady(true); setExcelFile(data.xlsx || "");
    setNotice({ type: "success", text: data.message });
  });

  const createAudio = () => run("Creating multilingual audio narration and YouTube SRT captions…", async () => {
    await withLiveStats(async () => {
      const data = await api("/api/audio", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issue_key: issueKey }) });
      setStats(data.stats); setNotice({ type: "success", text: data.message });
    });
  });

  const createVideos = () => run("Rendering 1080p videos with title cards…", async () => {
    await withLiveStats(async () => {
      const data = await api("/api/video", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issue_key: issueKey }) });
      setStats(data.stats); setNotice({ type: "success", text: data.message });
    });
  });

  const loadLinks = () => run("Loading YouTube link rows…", async () => {
    const data = await api(`/api/links/${issueKey}`); setLinks(data.rows);
  });

  const createQr = () => run("Creating QR codes…", async () => {
    const data = await api("/api/qr", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issue_key: issueKey, rows: links }) });
    setStats(data.stats); keepGoogleLinks(data); setNotice({ type: "success", text: data.message });
  });

  const publishGoogle = () => run("Uploading audio, videos, captions and QR codes to Google Drive and updating the Google Sheet…", async () => {
    const data = await api("/api/google/publish", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issue_key: issueKey }) });
    setStats(data.stats || stats); keepGoogleLinks(data); setNotice({ type: "success", text: data.message });
  });

  const patchRow = (index, field, value) => setRows(currentRows => currentRows.map((row, i) => i === index ? { ...row, [field]: value } : row));
  const patchLink = (index, value) => setLinks(currentLinks => currentLinks.map((row, i) => i === index ? { ...row, youtube_url: value } : row));

  const selectAllRows = (status) => {
    setRows(currentRows => currentRows.map(row => ({ ...row, selected: status ? "yes" : "no" })));
  };

  return <main className="shell">
    <header>
      <div className="header-left">
        <img src={mcciaLogo} alt="MCCIA Logo" className="brand-logo" />
        <div className="brand-divider"></div>
        <div>
          <span className="eyebrow">MCCIA · monthly production workspace</span>
          <h1>Sampada Media Studio</h1>
          <p>Convert selectable-text magazine articles into reviewable metadata, multilingual narration, 1080p videos, SRT captions, and print-ready QR codes.</p>
        </div>
      </div>
    </header>

    <section className="setup card">
      <div className="section-title"><FileText size={19}/><div><h2>Edition setup</h2><p>Select the issue edition, Google Drive sync destination, and source PDF.</p></div></div>
      <div className="setup-grid">
        <label>Year<input type="number" value={year} min="2000" max="2100" onChange={e => setYear(Number(e.target.value))}/></label>
        <label>Month<select value={month} onChange={e => setMonth(Number(e.target.value))}>{months.map((name, index) => <option key={name} value={index + 1}>{name}</option>)}</select></label>
        <label className="drive">Drive folder<input value={driveRoot} onChange={e => setDriveRoot(e.target.value)}/></label>
      </div>
      <label className={`dropzone ${file ? "has-file" : ""}`}>
        <Upload size={24}/>
        <input type="file" accept="application/pdf" onChange={e => setFile(e.target.files?.[0] || null)}/>
        <span>{file ? file.name : "Choose or drop the monthly Sampada PDF"}</span>
        <small>{file ? `${(file.size / 1048576).toFixed(1)} MB` : "PDF up to 250 MB"}</small>
      </label>
      <button className="primary" disabled={!file || busy} onClick={analyze}><Play size={17}/> Analyze edition</button>
    </section>

    {progress && <div className="progress-card">
      <div className="progress-label">{progress.phase === "upload"
        ? `Uploading PDF… ${mb(progress.loaded)} / ${mb(progress.total)} MB (${Math.round(progress.loaded / progress.total * 100)}%)`
        : "Upload complete. Reading pages and finding QR articles…"}</div>
      <div className={`progress-track ${progress.phase === "analyze" ? "indeterminate" : ""}`}>
        <div className="progress-fill" style={{ width: progress.phase === "upload" ? `${Math.round(progress.loaded / progress.total * 100)}%` : "100%" }}/>
      </div>
    </div>}
    {busy && <div className="busy"><LoaderCircle className="spin" size={19}/>{busy}{busy.includes("audio") && ` ${stats.audio} / ${stats.selected}`}{busy.includes("videos") && ` ${stats.videos} / ${stats.selected}`}</div>}
    {notice && <div className={`notice ${notice.type}`}>{notice.text}</div>}

    {issueKey && <>
      <section className="metrics">
        {[['Selected', selectedCount, null], ['Audio', stats.audio, stats.selected], ['Videos', stats.videos, stats.selected], ['QR codes', stats.qr, stats.selected]].map(([label, value, total], idx) => <div className="metric" key={label} style={{ "--i": idx }}><strong><CountUp value={value}/>{total !== null && <small> / {total}</small>}</strong><span>{label}</span></div>)}
      </section>

      <section className="card review">
        <div className="section-title">
          <Sheet size={19}/>
          <div style={{ flex: 1 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h2>QR-enabled article review</h2>
              <div style={{ display: "flex", gap: "8px" }}>
                <button type="button" onClick={() => selectAllRows(true)} style={{ padding: "4px 8px", fontSize: "12px" }}>
                  <CheckSquare size={13}/> Select All
                </button>
                <button type="button" onClick={() => selectAllRows(false)} style={{ padding: "4px 8px", fontSize: "12px" }}>
                  <Square size={13}/> Deselect All
                </button>
              </div>
            </div>
            <p>Confirm the selection and titles. Multilingual voices automatically route to English (`en-IN-NeerjaNeural`) or Marathi (`mr-IN-AarohiNeural`).</p>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: "60px" }}>Create</th>
                <th>Title</th>
                <th>Language / Voice</th>
                <th>Pages</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => <tr key={row.id}>
                <td><input type="checkbox" checked={String(row.selected).toLowerCase() === "yes" || row.selected === true} onChange={e => patchRow(index, 'selected', e.target.checked ? 'yes' : 'no')}/></td>
                <td><input className="cell-input title-input" value={row.title} onChange={e => patchRow(index, 'title', e.target.value)}/></td>
                <td><span className="reason">{row.language === "mr" ? "Marathi · Aarohi Neural" : "English · Neerja Neural"}</span></td>
                <td>{row.printed_start_page}–{row.printed_end_page}</td>
                <td><span className="reason">{row.selection_reason}</span></td>
              </tr>)}
            </tbody>
          </table>
        </div>
        <div className="actions">
          <button className="success" disabled={!selectedCount || busy} onClick={runFullAutomation}>
            <Sparkles size={16}/> 1-Click: audio, videos, Drive upload & Google Sheet
          </button>
          <button disabled={!selectedCount || busy} onClick={saveReview}><Sheet size={16}/> Create Excel & scripts</button>
          <button disabled={!scriptsReady || busy} onClick={createAudio}><Headphones size={16}/> Create audio & captions</button>
          <button disabled={!stats.audio || busy} onClick={createVideos}><Video size={16}/> Create videos</button>
          <button disabled={!stats.videos || busy || !google.configured} title={google.configured ? "" : google.help} onClick={publishGoogle}><Upload size={16}/> Upload to Google Drive & Sheet</button>
          <button disabled={busy} onClick={loadLinks}><QrCode size={16}/> YouTube links</button>
        </div>
      </section>

      {google.configured === false && <p className="folder"><span>Google Drive / Sheet upload is off: {google.help}</span></p>}
      {googleLinks.sheet_url && <p className="folder"><Sheet size={15}/><span>Google Sheet: <a href={googleLinks.sheet_url} target="_blank" rel="noreferrer">{googleLinks.sheet_url}</a> · Drive folder: <a href={googleLinks.drive_folder_url} target="_blank" rel="noreferrer">open</a></span></p>}
      {excelFile && <p className="folder"><Sheet size={15}/><span>Excel workbook saved to: {excelFile}</span></p>}

      {links.length > 0 && <section className="card links">
        <div className="section-title"><QrCode size={19}/><div><h2>YouTube links</h2><p>Paste each uploaded YouTube URL, then generate the QR codes (they are added to the Google Sheet and Drive).</p></div></div>
        {links.map((row, index) => <label key={row.id}><span>{row.title}</span><input placeholder="https://youtu.be/…" value={row.youtube_url || ''} onChange={e => patchLink(index, e.target.value)}/></label>)}
        <button className="primary" disabled={busy} onClick={createQr}><QrCode size={16}/> Create QR codes</button>
      </section>}

      {folder && <p className="folder"><FolderOpen size={15}/><span>Local output folder: {folder}</span></p>}
    </>}
  </main>;
}

const container = document.getElementById("root");
const root = container.__sampadaRoot || (container.__sampadaRoot = createRoot(container));
root.render(<React.StrictMode><App/></React.StrictMode>);
