on findFolder(folderName)
    set matchingFolders to {}
    tell application "Notes"
        repeat with accountItem in accounts
            repeat with folderItem in folders of accountItem
                if (name of folderItem as text) is folderName then set end of matchingFolders to contents of folderItem
            end repeat
        end repeat
    end tell
    if (count of matchingFolders) > 1 then error "APPLE_NOTES_DESTINATION_AMBIGUOUS"
    if (count of matchingFolders) is 1 then return item 1 of matchingFolders
    error "APPLE_NOTES_FOLDER_NOT_FOUND"
end findFolder

on run argv
    if (count of argv) is less than 5 then error "APPLE_NOTES_ARGUMENT_ERROR"
    set actionName to item 1 of argv
    set noteIdentifier to item 2 of argv
    set folderName to item 3 of argv
    set titleValue to item 4 of argv
    set bodyHTML to item 5 of argv

    tell application "Notes"
        if actionName is "create" then
            if folderName is "" then
                set targetAccount to first account
                set targetFolder to first folder of targetAccount
            else
                set targetFolder to my findFolder(folderName)
            end if
            -- No name property: Notes takes the title from the body's <h1>; passing both repeats it.
            set selectedNote to make new note at targetFolder with properties {body:bodyHTML}
        else
            try
                set selectedNote to note id noteIdentifier
                get name of selectedNote
            on error
                error "APPLE_NOTES_NOTE_NOT_FOUND"
            end try
            if actionName is "append_plain" or actionName is "restore_plain" then
                if (count of argv) is not 6 then error "APPLE_NOTES_ARGUMENT_ERROR"
                if (count of attachments of selectedNote) is not 0 then error "APPLE_NOTES_HAS_ATTACHMENTS"
                if (body of selectedNote as text) is not (item 6 of argv) then error "APPLE_NOTES_CONCURRENT_CHANGE"
                if actionName is "append_plain" then
                    set body of selectedNote to (body of selectedNote) & bodyHTML
                else
                    set body of selectedNote to bodyHTML
                end if
                return (id of selectedNote) & tab & (name of selectedNote)
            end if
            if actionName is "export" then
                -- Copy each file attachment out of the note (titleValue is the destination
                -- folder); tables and other body-native items cannot be saved and are reported.
                set seenIDs to {}
                set exportLines to {}
                set attachmentIndex to 0
                repeat with candidate in attachments of selectedNote
                    set attachmentIndex to attachmentIndex + 1
                    set candidateID to id of candidate as text
                    if candidateID is not in seenIDs then
                        set end of seenIDs to candidateID
                        set candidateName to ""
                        try
                            set candidateName to name of candidate as text
                        end try
                        if candidateName is "missing value" then set candidateName to ""
                        set targetPath to titleValue & "/" & attachmentIndex
                        try
                            save candidate in POSIX file targetPath
                            set end of exportLines to (attachmentIndex as text) & tab & candidateName & tab & targetPath
                        on error
                            set end of exportLines to (attachmentIndex as text) & tab & candidateName & tab & ""
                        end try
                    end if
                end repeat
                set AppleScript's text item delimiters to linefeed
                set exportText to exportLines as text
                set AppleScript's text item delimiters to ""
                return exportText
            end if
            if actionName is "plaintext" then
                return plaintext of selectedNote
            end if
            if actionName is "snapshot" then
                return (name of selectedNote) & tab & (body of selectedNote)
            end if
            if actionName is "restore" then
                set body of selectedNote to bodyHTML
                return (id of selectedNote) & tab & (name of selectedNote)
            end if
            if actionName is "created" then
                set createdText to (creation date of selectedNote) as «class isot» as string
                set folderText to ""
                try
                    set noteFolder to container of selectedNote
                    set folderText to (get name of noteFolder) as text
                end try
                return createdText & tab & folderText
            end if
            if actionName is "update" then
                set body of selectedNote to bodyHTML
            else if actionName is "attach" then
                -- Append only; do not reassign the existing body.
                get id of selectedNote
            else if actionName is "move" then
                set targetFolder to my findFolder(folderName)
                set targetFolderIdentifier to id of targetFolder as text
                move selectedNote to targetFolder
                -- Re-resolve the original ID; a receipt must describe the actual destination.
                set selectedNote to note id noteIdentifier
                set actualFolder to get container of selectedNote
                if (id of actualFolder as text) is not targetFolderIdentifier then error "APPLE_NOTES_MOVE_NOT_CONFIRMED"
            else if actionName is "delete" then
                set resultTitle to name of selectedNote
                delete selectedNote
                return noteIdentifier & tab & resultTitle
            else
                error "APPLE_NOTES_ACTION_INVALID"
            end if
        end if
        if (actionName is "create" or actionName is "update" or actionName is "attach") and (count of argv) is greater than 5 then
            delay 8
            repeat with argumentIndex from 6 to (count of argv)
                set attachmentPath to item argumentIndex of argv
                set priorIDs to id of attachments of selectedNote
                set insertedAttachment to make new attachment at selectedNote with data (POSIX file attachmentPath)
                set keepID to id of insertedAttachment
                set keepName to name of insertedAttachment
                delay 0.5
                -- Never delete a pre-existing ID or an asynchronously imported other file.
                set currentAttachments to attachments of selectedNote
                repeat with candidate in currentAttachments
                    set candidateID to id of candidate
                    if candidateID is not in priorIDs and candidateID is not keepID then
                        if (name of candidate) is keepName then delete candidate
                    end if
                end repeat
                if keepID is not in priorIDs then
                    set sameIDCount to count of (attachments of selectedNote whose id is keepID)
                    repeat while sameIDCount > 1
                        delete first attachment of selectedNote whose id is keepID
                        set newCount to count of (attachments of selectedNote whose id is keepID)
                        if newCount >= sameIDCount then error "APPLE_NOTES_DUPLICATE_ID_UNRESOLVED"
                        set sameIDCount to newCount
                    end repeat
                end if
            end repeat
        end if
        set finalNames to name of attachments of selectedNote
        set AppleScript's text item delimiters to linefeed
        set namesText to finalNames as text
        set AppleScript's text item delimiters to ""
        return (id of selectedNote) & tab & (name of selectedNote) & tab & (count of finalNames) & tab & namesText
    end tell
end run
