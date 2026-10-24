function run(argv) {
    const query = String(argv[0] || "").trim().toLocaleLowerCase();
    const folderFilter = String(argv[1] || "").trim().toLocaleLowerCase();
    const limit = Number(argv[2] || 10);
    const includeBody = String(argv[3]) === "true";
    const searchBody = String(argv[4]) === "true";
    const summary = String(argv[5]) === "true";
    const offset = Number(argv[6] || 0);
    const noteId = String(argv[7] || "");
    const saveImages = String(argv[8]) === "true";
    const excludedFolders = new Set(["recently deleted", "최근 삭제된 항목"]);
    const results = [], counts = [];
    let matched = 0;
    for (const folder of Application("Notes").folders()) {
        const folderName = String(folder.name());
        if (excludedFolders.has(folderName.toLocaleLowerCase())) continue;
        if (folderFilter && !folderName.toLocaleLowerCase().includes(folderFilter)) continue;
        // One bulk ID request per folder; summary never reads titles or bodies.
        const ids = folder.notes.id();
        if (summary) { counts.push({folder: folderName, count: ids.length}); continue; }
        const titles = folder.notes.name();
        const bodies = searchBody && query ? folder.notes.plaintext() : null;
        if (titles.length !== ids.length || (bodies && titles.length !== bodies.length))
            throw new Error("Notes changed during lookup; retry the search");
        for (let i = 0; i < titles.length; i++) {
            if (noteId && String(ids[i]) !== noteId) continue;
            const title = String(titles[i]);
            if (query && !title.toLocaleLowerCase().includes(query) &&
                !(bodies && String(bodies[i]).toLocaleLowerCase().includes(query))) continue;
            if (matched++ < offset) continue;
            if (results.length === limit)
                return JSON.stringify({items: results, next_offset: offset + results.length});
            const row = {stableNoteIdentifier: String(ids[i]), note_id: String(ids[i]), folder: folderName, title};
            const note = folder.notes.byId(ids[i]);
            row.image_count = 0;
            try {
                const html = String(note.body());
                const images = [];
                const clean = html.replace(/(<img\b[^>]*\bsrc\s*=\s*)(["'])([\s\S]*?)\2/gi,
                    function(all, prefix, quote, src) { images.push(src); return prefix + quote + quote; });
                row.image_count = images.length;
                row.images_available = true;
                if (includeBody || noteId) row._html = clean.replace(/data:[^"'<>\s]+/gi, '');
                if (saveImages) row._images = images;
            } catch (e) { row.images_available = false; }
            if (includeBody || noteId) {
                row.body = bodies ? String(bodies[i]) : String(note.plaintext());
                row.bodyAvailable = true;
            }
            results.push(row);
        }
    }
    if (summary) return JSON.stringify({folders: counts, total: counts.reduce((sum, f) => sum + f.count, 0)});
    return JSON.stringify({items: results, next_offset: null});
}
