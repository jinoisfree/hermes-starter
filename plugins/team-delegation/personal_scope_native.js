// Read metadata only. Never create, move or edit an item from this resolver.
function run(argv) {
    const input = JSON.parse(argv[0]);
    if (input.domain === 'notes') {
        const app = Application('Notes');
        const folders = [];
        app.accounts().forEach(a => a.folders().forEach(f => folders.push({
            id: f.id(), name: f.name(), account: a.name(), account_id: a.id()
        })));
        let current = null;
        if (input.id) {
            const note = app.notes.byId(input.id);
            const parent = note.container();
            const found = folders.filter(f => f.id === parent.id());
            if (found.length !== 1) throw Error('scope_note_folder_unverified');
            current = found[0];
        }
        return JSON.stringify({folders: folders, current: current});
    }
    if (input.domain === 'todo') {
        const app = Application('Reminders');
        const lists = app.lists().map(l => ({id: l.id(), name: l.name(), color: l.color()}));
        let current = null;
        if (input.id) {
            const reminder = app.reminders.byId(input.id);
            const found = lists.filter(l => l.id === reminder.container().id());
            if (found.length !== 1) throw Error('scope_reminder_list_unverified');
            current = found[0];
        }
        return JSON.stringify({lists: lists, current: current});
    }
    throw Error('scope_domain_invalid');
}
