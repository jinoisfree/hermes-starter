function run(argv) {
    const id = String(argv[0]);
    const note = Application("Notes").notes.byId(id);
    const result = {note_id: String(note.id()), title: String(note.name()), body_html: null, plaintext: null, attachment_names: null};
    try { result.plaintext = String(note.plaintext()); } catch (error) {}
    try { result.body_html = String(note.body()); } catch (error) {}
    try { result.attachment_names = note.attachments.name().map(name => name == null ? "" : String(name)); }
    catch (error) {}
    return JSON.stringify(result);
}
