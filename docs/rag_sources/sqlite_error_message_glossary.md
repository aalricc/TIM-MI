Introduction
SQLite Error Message Glossary (message text to result code)
Note: supplemental lookup table, not an sqlite.org page. It maps the message text that sqlite3_errmsg() and the sqlite3 CLI print to the result code names documented in "Result and Error Codes". Verify details against that document.
Python's sqlite3 module raises these as sqlite3.OperationalError (for example "database is locked") or sqlite3.DatabaseError (for example "database disk image is malformed" or "file is not a database"). The message text is the same.

(1) SQLITE_ERROR
Error message text: "SQL logic error"
Generic error. Also used for problems such as SQL syntax errors or a missing table.
Related documents: Result and Error Codes.

(3) SQLITE_PERM
Error message text: "access permission denied"
The requested access mode for a newly created database could not be provided.
Related documents: Result and Error Codes.

(5) SQLITE_BUSY
Error message text: "database is locked"
Another connection or process holds a conflicting lock on the database file, so the operation could not continue. This is the usual meaning of "database is locked".
Related documents: File Locking And Concurrency In SQLite Version 3; Write-Ahead Logging; Atomic Commit In SQLite; Result and Error Codes.

(6) SQLITE_LOCKED
Error message text: "database table is locked"
A conflict inside a single database connection, or between connections using shared cache. Different from SQLITE_BUSY.
Related documents: Result and Error Codes.

(7) SQLITE_NOMEM
Error message text: "out of memory"
SQLite could not allocate the memory it needed.
Related documents: Result and Error Codes.

(8) SQLITE_READONLY
Error message text: "attempt to write a readonly database"
A write was attempted on a database that cannot be written to, for example because of file permissions, a read-only open mode, or a database file that moved or was deleted while open.
Related documents: Result and Error Codes; How To Corrupt An SQLite Database File.

(9) SQLITE_INTERRUPT
Error message text: "interrupted"
The operation was stopped by a call to sqlite3_interrupt().
Related documents: Result and Error Codes.

(10) SQLITE_IOERR
Error message text: "disk I/O error"
A disk or file I/O error occurred. Extended codes such as SQLITE_IOERR_SHORT_READ or SQLITE_IOERR_FSYNC give the detail, but the message text stays the same, so check the extended error code.
Related documents: Result and Error Codes; How To Corrupt An SQLite Database File.

(11) SQLITE_CORRUPT
Error message text: "database disk image is malformed"
The database file is corrupted. Extended codes such as SQLITE_CORRUPT_INDEX (779) usually produce the same message text, so check sqlite3_extended_errcode() to tell them apart. For a corrupt index, REINDEX is the documented fix. For wider damage, see the recovery document.
Related documents: Recovering Data From A Corrupt SQLite Database; How To Corrupt An SQLite Database File; Command Line Shell For SQLite (the .recover command); REINDEX; Result and Error Codes.

(13) SQLITE_FULL
Error message text: "database or disk is full"
A write failed because the disk or the database is full.
Related documents: Result and Error Codes.

(14) SQLITE_CANTOPEN
Error message text: "unable to open database file"
SQLite could not open the database file or a related file (journal, WAL or temporary file).
Related documents: Result and Error Codes; Write-Ahead Logging.

(15) SQLITE_PROTOCOL
Error message text: "locking protocol"
A problem with the file locking protocol, which can occur in WAL mode when many connections compete.
Related documents: Result and Error Codes; Write-Ahead Logging; File Locking And Concurrency In SQLite Version 3.

(17) SQLITE_SCHEMA
Error message text: "database schema has changed"
The database schema changed after a statement was prepared.
Related documents: Result and Error Codes.

(18) SQLITE_TOOBIG
Error message text: "string or blob too big"
A string or BLOB exceeded the size limit.
Related documents: Result and Error Codes.

(19) SQLITE_CONSTRAINT
Error message text: "constraint failed"
A constraint such as UNIQUE, NOT NULL or FOREIGN KEY was violated. The detail usually follows in the message.
Related documents: Result and Error Codes; Transaction.

(21) SQLITE_MISUSE
Error message text: "bad parameter or other API misuse"
The library was used incorrectly, for example a connection was used after being closed.
Related documents: Result and Error Codes.

(23) SQLITE_AUTH
Error message text: "authorization denied"
An authorizer callback refused the operation.
Related documents: Result and Error Codes.

(26) SQLITE_NOTADB
Error message text: "file is not a database"
The file opened is not an SQLite database, or its header is unreadable or damaged.
Related documents: Database File Format; Recovering Data From A Corrupt SQLite Database; How To Corrupt An SQLite Database File; Result and Error Codes.

