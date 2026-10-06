import { useEffect, useRef, useState } from "react";
import { API_URL, api, errorMessage, type ExtractionResult } from "./api";

// Upload a PDF, check each page with its set boxes, fix the JSON, save the correction.
// The document's hash goes in the URL (?doc=...), so a reload fetches the stored
// result instead of extracting again.
export default function App() {
  const [result, setResult] = useState<ExtractionResult | null>(null);
  const [json, setJson] = useState("");
  const [page, setPage] = useState<number | null>(null);
  const [imageVersion, setImageVersion] = useState(0);
  const [status, setStatus] = useState("");
  const [uploading, setUploading] = useState(false);
  const editor = useRef<HTMLTextAreaElement>(null);

  function show(loaded: ExtractionResult) {
    setResult(loaded);
    setJson(JSON.stringify(loaded, null, 2));
    setPage(loaded.sets[0]?.location[0]?.page ?? loaded.hardware_pages[0] ?? null);
    setImageVersion((version) => version + 1);
    history.replaceState(null, "", `?doc=${loaded.pdf_hash}`);
  }

  useEffect(() => {
    const pdfHash = new URLSearchParams(location.search).get("doc");
    if (!pdfHash) return;
    api
      .GET("/documents/{pdf_hash}", { params: { path: { pdf_hash: pdfHash } } })
      .then(({ data, error }) => (data ? show(data) : setStatus(errorMessage(error))));
  }, []);

  // Scroll the JSON to the first set on the page being shown: the set's index is
  // the number of "set_number" keys to skip.
  useEffect(() => {
    const setIndex =
      result?.sets.findIndex((set) => set.location.some((location) => location.page === page)) ?? -1;
    if (setIndex < 0 || !editor.current) return;
    let position = -1;
    for (let skipped = 0; skipped <= setIndex; skipped++) {
      position = json.indexOf('"set_number"', position + 1);
    }
    const line = json.slice(0, position).split("\n").length - 1;
    editor.current.scrollTop = line * parseFloat(getComputedStyle(editor.current).lineHeight);
  }, [page, result]);

  async function upload(file: File) {
    setUploading(true);
    setStatus("");
    // The schema types the upload as a string; it is sent as multipart form data.
    const { data, error } = await api.POST("/extract", {
      body: { file: file as unknown as string },
      bodySerializer: (body) => {
        const form = new FormData();
        form.append("file", body.file as unknown as File);
        return form;
      },
    });
    setUploading(false);
    if (!data) return setStatus(errorMessage(error));
    show(data);
    setStatus(data.corrected ? "Loaded your saved correction." : "");
  }

  async function saveCorrection() {
    if (!result?.pdf_hash) return;
    let corrected;
    try {
      corrected = JSON.parse(json);
    } catch (error) {
      return setStatus(`Invalid JSON: ${(error as Error).message}`);
    }
    const { data, error } = await api.PUT("/documents/{pdf_hash}/corrections", {
      params: { path: { pdf_hash: result.pdf_hash } },
      body: corrected,
    });
    if (!data) return setStatus(errorMessage(error));
    show(data);
    setStatus("Correction saved.");
  }

  function download() {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([json], { type: "application/json" }));
    link.download = `${result?.filename ?? "result"}.json`;
    link.click();
  }

  const pageIndex = result && page ? result.hardware_pages.indexOf(page) : -1;
  const setsOnPage = result?.sets.filter((set) =>
    set.location.some((location) => location.page === page),
  );

  return (
    <main>
      <header>
        <h1>Fresco Hardware Sets</h1>
        <input
          type="file"
          accept="application/pdf"
          disabled={uploading}
          onChange={(event) => event.target.files?.[0] && upload(event.target.files[0])}
        />
        <span className="status">{status}</span>
      </header>

      {result && (
        <>
          <p>
            {result.filename}: {result.sets.length} sets on {result.hardware_pages.length}{" "}
            pages{result.corrected && " (corrected)"}
            {result.warnings.map((warning) => (
              <span key={warning} className="warning">
                {" "}
                · {warning}
              </span>
            ))}
          </p>
          <div className="columns">
            <section>
              <nav>
                <button
                  disabled={pageIndex <= 0}
                  onClick={() => setPage(result.hardware_pages[pageIndex - 1])}
                >
                  ←
                </button>
                <select
                  value={page ?? ""}
                  onChange={(event) => setPage(Number(event.target.value))}
                >
                  {result.hardware_pages.map((pageNumber) => (
                    <option key={pageNumber} value={pageNumber}>
                      Page {pageNumber}
                    </option>
                  ))}
                </select>
                <button
                  disabled={pageIndex >= result.hardware_pages.length - 1}
                  onClick={() => setPage(result.hardware_pages[pageIndex + 1])}
                >
                  →
                </button>
                <span>
                  Sets: {setsOnPage?.map((set) => set.set_number ?? "?").join(", ") || "none"}
                </span>
              </nav>
              {page && (
                <img
                  src={`${API_URL}/documents/${result.pdf_hash}/pages/${page}.png?v=${imageVersion}`}
                  alt={`Page ${page}`}
                />
              )}
            </section>
            <section>
              <nav>
                <button onClick={saveCorrection}>Save correction</button>
                <button onClick={download}>Download JSON</button>
              </nav>
              <textarea
                ref={editor}
                value={json}
                onChange={(event) => setJson(event.target.value)}
                spellCheck={false}
              />
            </section>
          </div>
        </>
      )}
    </main>
  );
}
