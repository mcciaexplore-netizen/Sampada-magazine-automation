import React, { useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { FileText, FolderOpen, Headphones, LoaderCircle, Play, QrCode, Sheet, Upload, Video } from "lucide-react";
import "./styles.css";

const months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const current = new Date();

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.message || "Request failed");
  return data;
}

function App() {
  const [year, setYear] = useState(2026);
  const [month, setMonth] = useState(8);
  const [driveRoot, setDriveRoot] = useState("C:\\Users\\Aarushi Gupta\\Documents\\Codex\\Sampada Drive");
  const [file, setFile] = useState(null);
  const [issueKey, setIssueKey] = useState("");
  const [rows, setRows] = useState([]);
  const [links, setLinks] = useState([]);
  const [stats, setStats] = useState({ selected: 0, audio: 0, videos: 0, qr: 0 });
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState(null);
  const [folder, setFolder] = useState("");

  const selectedCount = useMemo(() => rows.filter(row => String(row.selected).toLowerCase() === "yes" || row.selected === true).length, [rows]);
  const scriptsReady = Boolean(issueKey && notice?.scriptsReady);
  const allAudio = stats.selected > 0 && stats.audio >= stats.selected;

  const run = async (label, work) => {
    setBusy(label); setNotice(null);
    try { await work(); }
    catch (error) { setNotice({ type: "error", text: error.message }); }
    finally { setBusy(""); }
  };

  const analyze = () => run("Analyzing the magazine and scanning every page for QR codes…", async () => {
    if (!file) throw new Error("Choose a Sampada PDF first");
    const body = new FormData();
    body.append("file", file); body.append("year", year); body.append("month", month); body.append("drive_root", driveRoot);
    const data = await api("/api/analyze", { method: "POST", body });
    setIssueKey(data.issue_key); setRows(data.rows); setStats(data.stats); setFolder(data.monthly_folder);
    setNotice({ type: data.fallback ? "warning" : "success", text: data.fallback ? `Proof PDF: no embedded QR images. Review ${data.stats.selected} fallback selections.` : `Found ${data.stats.selected} QR-enabled articles.` });
  });

  const saveReview = () => run("Creating Excel and narration scripts…", async () => {
    const data = await api("/api/review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issue_key: issueKey, rows }) });
    setStats(data.stats); setNotice({ type: "success", text: data.message, scriptsReady: true });
  });

  const createAudio = () => run("Creating multilingual narration…", async () => {
    const data = await api("/api/audio", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issue_key: issueKey }) });
    setStats(data.stats); setNotice({ type: "success", text: data.message, scriptsReady: true });
  });

  const createVideos = () => run("Rendering 1080p videos…", async () => {
    const data = await api("/api/video", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issue_key: issueKey }) });
    setStats(data.stats); setNotice({ type: "success", text: data.message, scriptsReady: true });
  });

  const loadLinks = () => run("Loading YouTube link rows…", async () => {
    const data = await api(`/api/links/${issueKey}`); setLinks(data.rows);
  });

  const createQr = () => run("Creating QR codes…", async () => {
    const data = await api("/api/qr", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ issue_key: issueKey, rows: links }) });
    setStats(data.stats); setNotice({ type: "success", text: data.message, scriptsReady: true });
  });

  const patchRow = (index, field, value) => setRows(currentRows => currentRows.map((row, i) => i === index ? { ...row, [field]: value } : row));
  const patchLink = (index, value) => setLinks(currentLinks => currentLinks.map((row, i) => i === index ? { ...row, youtube_url: value } : row));

  return <main className="shell">
    <header><span className="eyebrow">MCCIA · monthly production workspace</span><h1>Sampada media studio</h1><p>Turn every QR-enabled magazine article into a complete YouTube media package.</p></header>

    <section className="setup card">
      <div className="section-title"><FileText size={19}/><div><h2>Edition setup</h2><p>Select the issue and its Google Drive destination.</p></div></div>
      <div className="setup-grid">
        <label>Year<input type="number" value={year} min="2000" max="2100" onChange={e => setYear(Number(e.target.value))}/></label>
        <label>Month<select value={month} onChange={e => setMonth(Number(e.target.value))}>{months.map((name, index) => <option key={name} value={index + 1}>{name}</option>)}</select></label>
        <label className="drive">Drive folder<input value={driveRoot} onChange={e => setDriveRoot(e.target.value)}/></label>
      </div>
      <label className={`dropzone ${file ? "has-file" : ""}`}><Upload size={24}/><input type="file" accept="application/pdf" onChange={e => setFile(e.target.files?.[0] || null)}/><span>{file ? file.name : "Choose or drop the monthly Sampada PDF"}</span><small>{file ? `${(file.size / 1048576).toFixed(1)} MB` : "PDF up to 250 MB"}</small></label>
      <button className="primary" disabled={!file || busy} onClick={analyze}><Play size={17}/> Analyze edition</button>
    </section>

    {busy && <div className="busy"><LoaderCircle className="spin" size={19}/>{busy}</div>}
    {notice && <div className={`notice ${notice.type}`}>{notice.text}</div>}

    {issueKey && <>
      <section className="metrics">
        {[['Selected', selectedCount], ['Audio', stats.audio], ['Videos', stats.videos], ['QR codes', stats.qr]].map(([label, value]) => <div className="metric" key={label}><strong>{value}</strong><span>{label}</span></div>)}
      </section>

      <section className="card review">
        <div className="section-title"><Sheet size={19}/><div><h2>QR-enabled article review</h2><p>Confirm the selection and titles. All narration uses the fast English neural voice.</p></div></div>
        <div className="table-wrap"><table><thead><tr><th>Create</th><th>Title</th><th>Language</th><th>Pages</th><th>Reason</th></tr></thead><tbody>{rows.map((row, index) => <tr key={row.id}>
          <td><input type="checkbox" checked={String(row.selected).toLowerCase() === "yes" || row.selected === true} onChange={e => patchRow(index, 'selected', e.target.checked ? 'yes' : 'no')}/></td>
          <td><input className="cell-input title-input" value={row.title} onChange={e => patchRow(index, 'title', e.target.value)}/></td>
          <td><span className="reason">English · Neerja Neural</span></td>
          <td>{row.printed_start_page}–{row.printed_end_page}</td><td><span className="reason">{row.selection_reason}</span></td>
        </tr>)}</tbody></table></div>
        <div className="actions"><button className="primary" disabled={!selectedCount || busy} onClick={saveReview}><Sheet size={16}/> Create Excel & scripts</button><button disabled={!scriptsReady || busy} onClick={createAudio}><Headphones size={16}/> Create audio</button><button disabled={!allAudio || busy} onClick={createVideos}><Video size={16}/> Create videos</button><button disabled={busy} onClick={loadLinks}><QrCode size={16}/> YouTube links</button></div>
      </section>

      {links.length > 0 && <section className="card links"><div className="section-title"><QrCode size={19}/><div><h2>YouTube links</h2><p>Paste each uploaded video URL, then generate matching QR codes.</p></div></div>{links.map((row, index) => <label key={row.id}><span>{row.title}</span><input placeholder="https://youtu.be/…" value={row.youtube_url || ''} onChange={e => patchLink(index, e.target.value)}/></label>)}<button className="primary" disabled={busy} onClick={createQr}><QrCode size={16}/> Create QR codes</button></section>}

      {folder && <p className="folder"><FolderOpen size={15}/><span>{folder}</span></p>}
    </>}
  </main>;
}

createRoot(document.getElementById("root")).render(<React.StrictMode><App/></React.StrictMode>);
