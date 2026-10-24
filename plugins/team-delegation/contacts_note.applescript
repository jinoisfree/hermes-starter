on run argv
    set operation to item 1 of argv
    set contactID to item 2 of argv
    if operation is not "get" and operation is not "set" then error "Invalid Contacts note operation"
    if operation is "set" then set requestedNote to item 3 of argv
    tell application "Contacts"
        set p to person id contactID
        if operation is "set" then
            set note of p to requestedNote
            save
        end if
        set actualNote to note of p
        if actualNote is missing value then return ""
        return actualNote
    end tell
end run
