// Only matched notes fetch HTML; search still reads plaintext columns once per folder.
function readHTML(note, row) {
    try {
        row._html = String(note.body()).replace(/(<img\b[^>]*\bsrc\s*=\s*)(["'])([\s\S]*?)\2/gi,
            function(all, prefix, quote) { return prefix + quote + quote; })
            .replace(/data:[^"'<>\s]+/gi, '');
    } catch (error) { /* Python marks the plaintext fallback as summary_readable=false. */ }
}

function run(argv) {
    const query = String(argv[0] || "").trim().toLocaleLowerCase();
    const folderFilter = String(argv[1] || "").trim().toLocaleLowerCase();
    const limit = Math.max(1, Math.min(20, Number(argv[2] || 10)));
    const includeBody = String(argv[3] || "false") === "true";
    const searchBody = String(argv[4] || "false") === "true";
    const excludedFolders = new Set(["recently deleted", "최근 삭제된 항목"]);
    const notesApp = Application("Notes");
    const results = [];

    for (const folder of notesApp.folders()) {
        const folderName = String(folder.name());
        if (excludedFolders.has(folderName.toLocaleLowerCase())) continue;
        if (folderFilter && !folderName.toLocaleLowerCase().includes(folderFilter)) continue;
        if (searchBody && query) {
            // Fetch columns once per folder. Per-note Apple Events take longer than the
            // tool's 30-second budget even for a small library. A failed read propagates
            // as a native lookup failure, never as a successful empty search.
            const titles = folder.notes.name();
            const bodies = folder.notes.plaintext();
            const ids = folder.notes.id();
            if (titles.length !== bodies.length || titles.length !== ids.length) {
                throw new Error("Notes changed during lookup; retry the search");
            }
            for (let i = 0; i < titles.length; i++) {
                const title = String(titles[i]), body = String(bodies[i]);
                if (!title.toLocaleLowerCase().includes(query) && !body.toLocaleLowerCase().includes(query)) continue;
                const row = {stableNoteIdentifier: String(ids[i]), note_id: String(ids[i]), folder: folderName, title};
                if (includeBody) {
                    row.body = body; row.bodyAvailable = true;
                    try { readHTML(folder.notes.byId(ids[i]), row); } catch (error) {}
                }
                results.push(row);
                if (results.length >= limit) return JSON.stringify(results);
            }
            continue;
        }
        for (const note of folder.notes()) {
            const title = String(note.name());
            if (query && !title.toLocaleLowerCase().includes(query)) continue;
            const row = {
                stableNoteIdentifier: String(note.id()),
                note_id: String(note.id()),
                folder: folderName,
                title: title,
            };
            if (includeBody) {
                readHTML(note, row);
                try {
                    row.body = String(note.plaintext());
                    row.bodyAvailable = true;
                } catch (error) {
                    row.bodyAvailable = false;
                }
            }
            results.push(row);
            if (results.length >= limit) return JSON.stringify(results);
        }
    }
    return JSON.stringify(results);
}
