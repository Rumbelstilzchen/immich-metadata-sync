#!/bin/bash
################################################################################
# Immich Backup Script (Container-Version)
# Läuft im immich-metadata-sync Container
# Optimiert für LXC mit vorhandenen Immich-DB-Backups
# Sichert Immich-Bilder und Datenbank auf USB-Festplatte
################################################################################

set -euo pipefail

# =============================================================================
# KONFIGURATION (aus Environment-Variablen)
# =============================================================================

# USB-Backup-Ziel (wird als Volume gemountet)
BACKUP_TARGET="${BACKUP_TARGET:-/backup}"

# Immich Library (bereits im Container gemountet)
IMMICH_LIBRARY_DIR="${IMMICH_PHOTO_DIR:-/library}"

# Vorhandene Immich-DB-Backups (optional)
IMMICH_DB_BACKUP_DIR="${IMMICH_DB_BACKUP_DIR:-/immich-backups}"

# Datenbank-Verbindung (aus Umgebungsvariablen oder Standard)
DB_HOST="${DB_HOST:-immich_postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_USERNAME="${DB_USERNAME:-postgres}"
DB_PASSWORD="${DB_PASSWORD:-postgres}"
DB_NAME="${DB_NAME:-immich}"

# Immich API (für Metadaten)
IMMICH_URL="${IMMICH_INSTANCE_URL:-}"
IMMICH_KEY="${IMMICH_API_KEY:-}"

# Backup-Einstellungen
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="${BACKUP_TARGET}/${TIMESTAMP}"
KEEP_BACKUPS="${KEEP_BACKUPS:-7}"
DB_BACKUP_MAX_AGE_DAYS="${DB_BACKUP_MAX_AGE_DAYS:-5}"  # Warnung ab 5 Tagen

# Log-Datei
LOG_FILE="${BACKUP_TARGET}/backup.log"

# =============================================================================
# FUNKTIONEN
# =============================================================================

log() {
    local message="[$(date '+%Y-%m-%d %H:%M:%S')] $1"
    echo "$message" | tee -a "$LOG_FILE"
}

error_exit() {
    log "❌ FEHLER: $1"
    exit 1
}

format_age() {
    local seconds=$1
    local days=$((seconds / 86400))
    local hours=$(((seconds % 86400) / 3600))
    local minutes=$(((seconds % 3600) / 60))
    
    if [ $days -gt 0 ]; then
        echo "${days}d ${hours}h ${minutes}m"
    elif [ $hours -gt 0 ]; then
        echo "${hours}h ${minutes}m"
    else
        echo "${minutes}m"
    fi
}

check_prerequisites() {
    log "Prüfe Voraussetzungen..."
    
    # Prüfe Backup-Ziel
    if [ ! -d "$BACKUP_TARGET" ]; then
        error_exit "Backup-Ziel '$BACKUP_TARGET' nicht gefunden. Ist die USB-Festplatte gemountet?"
    fi
    
    # Prüfe Schreibrechte
    if ! touch "${BACKUP_TARGET}/.writetest" 2>/dev/null; then
        error_exit "Keine Schreibrechte auf '$BACKUP_TARGET'"
    fi
    rm -f "${BACKUP_TARGET}/.writetest"
    
    # Prüfe Library-Verzeichnis
    if [ ! -d "$IMMICH_LIBRARY_DIR" ]; then
        error_exit "Library-Verzeichnis '$IMMICH_LIBRARY_DIR' nicht gefunden"
    fi
    
    # Prüfe ob rsync verfügbar ist
    if ! command -v rsync &> /dev/null; then
        error_exit "rsync ist nicht installiert"
    fi
    
    # Prüfe ob pg_dump verfügbar ist (nur wenn kein Immich-Backup-Dir)
    if [ ! -d "$IMMICH_DB_BACKUP_DIR" ]; then
        if ! command -v pg_dump &> /dev/null; then
            error_exit "pg_dump (PostgreSQL client) ist nicht installiert und kein Immich-Backup-Verzeichnis gefunden"
        fi
    fi
    
    log "✅ Alle Voraussetzungen erfüllt"
}

check_disk_space() {
    log "Prüfe verfügbaren Speicherplatz..."
    
    local required_space=$(du -sb "$IMMICH_LIBRARY_DIR" 2>/dev/null | cut -f1 || echo 0)
    local available_space=$(df -B1 "$BACKUP_TARGET" | tail -1 | awk '{print $4}')
    
    if [ "$required_space" -eq 0 ]; then
        log "⚠️  Warnung: Konnte Speicherplatzbedarf nicht ermitteln"
        return
    fi
    
    # 25% Puffer für DB und Kompression
    required_space=$((required_space * 125 / 100))
    
    if [ "$available_space" -lt "$required_space" ]; then
        local req_gb=$((required_space / 1024 / 1024 / 1024))
        local avail_gb=$((available_space / 1024 / 1024 / 1024))
        error_exit "Nicht genügend Speicherplatz (Benötigt: ~${req_gb}GB, Verfügbar: ${avail_gb}GB)"
    fi
    
    local avail_gb=$((available_space / 1024 / 1024 / 1024))
    log "✅ Ausreichend Speicherplatz verfügbar (~${avail_gb}GB)"
}

create_backup_structure() {
    log "Erstelle Backup-Verzeichnisstruktur..."
    
    mkdir -p "$BACKUP_DIR"/{database,library,metadata,logs} || error_exit "Konnte Backup-Verzeichnisse nicht erstellen"
    
    log "✅ Backup-Verzeichnis: $BACKUP_DIR"
}

test_database_connection() {
    log "Teste Datenbankverbindung..."
    
    export PGPASSWORD="$DB_PASSWORD"
    
    if ! psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USERNAME" -d "$DB_NAME" -c "SELECT version();" &>/dev/null; then
        log "⚠️  Warnung: Kann nicht mit Datenbank verbinden (Host: $DB_HOST:$DB_PORT)"
        log "   Versuche vorhandene Immich-Backups zu nutzen..."
        return 1
    fi
    
    log "✅ Datenbankverbindung erfolgreich"
    return 0
}

find_latest_immich_backup() {
    # Sucht das neueste Immich-DB-Backup und gibt Infos zurück
    
    if [ ! -d "$IMMICH_DB_BACKUP_DIR" ]; then
        return 1
    fi
    
    # Finde neuestes Backup (nach Änderungszeit sortiert)
    local latest_backup=$(find "$IMMICH_DB_BACKUP_DIR" -name "immich-db-backup-*.sql.gz" -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    
    if [ -z "$latest_backup" ] || [ ! -f "$latest_backup" ]; then
        return 1
    fi
    
    echo "$latest_backup"
    return 0
}

analyze_immich_backup() {
    local backup_file="$1"
    local backup_name=$(basename "$backup_file")
    
    # Extrahiere Datum aus Dateinamen (Format: immich-db-backup-20260119T020000-*.sql.gz)
    local date_str=$(echo "$backup_name" | grep -oP '\d{8}T\d{6}' | head -1)
    
    if [ -n "$date_str" ]; then
        # Parse YYYYMMDDTHHMMSS
        local year=${date_str:0:4}
        local month=${date_str:4:2}
        local day=${date_str:6:2}
        local hour=${date_str:9:2}
        local minute=${date_str:11:2}
        local second=${date_str:13:2}
        
        local backup_date="${year}-${month}-${day} ${hour}:${minute}:${second}"
        local backup_timestamp=$(date -d "$backup_date" +%s 2>/dev/null || stat -c %Y "$backup_file")
    else
        # Fallback: Nutze Datei-Änderungszeit
        local backup_timestamp=$(stat -c %Y "$backup_file")
        local backup_date=$(date -d "@$backup_timestamp" '+%Y-%m-%d %H:%M:%S')
    fi
    
    local current_timestamp=$(date +%s)
    local age_seconds=$((current_timestamp - backup_timestamp))
    local age_days=$((age_seconds / 86400))
    local age_formatted=$(format_age $age_seconds)
    
    local size=$(du -h "$backup_file" | cut -f1)
    
    echo "${backup_file}|${backup_date}|${age_seconds}|${age_days}|${age_formatted}|${size}"
}

backup_database() {
    log "🗄️  Starte Datenbank-Backup..."
    
    # Prüfe auf vorhandene Immich-Backups
    local use_existing=false
    local existing_backup=""
    
    if [ -d "$IMMICH_DB_BACKUP_DIR" ]; then
        log "   📂 Prüfe Immich-Backup-Verzeichnis: $IMMICH_DB_BACKUP_DIR"
        
        existing_backup=$(find_latest_immich_backup)
        
        if [ -n "$existing_backup" ]; then
            log "   📋 Immich-Backup gefunden: $(basename "$existing_backup")"
            
            # Analysiere Backup
            IFS='|' read -r backup_file backup_date age_seconds age_days age_formatted size <<< "$(analyze_immich_backup "$existing_backup")"
            
            log "   📅 Backup-Datum:  $backup_date"
            log "   ⏱️  Alter:         $age_formatted ($age_days Tage)"
            log "   💾 Größe:         $size"
            
            # Prüfe Alter
            local max_age_seconds=$((DB_BACKUP_MAX_AGE_DAYS * 86400))
            
            if [ "$age_days" -ge "$DB_BACKUP_MAX_AGE_DAYS" ]; then
                log "   ⚠️  WARNUNG: Backup ist älter als $DB_BACKUP_MAX_AGE_DAYS Tage!"
                log "   ⚠️  Empfehlung: Prüfe ob Immich-Backups noch laufen"
                
                # Erstelle trotzdem Backup, aber mit Warnung
                if [ "$age_days" -ge 10 ]; then
                    log "   ❌ Backup ist zu alt (>10 Tage), erstelle neues pg_dump..."
                    use_existing=false
                else
                    log "   ⚠️  Nutze vorhandenes Backup trotz Alter (5-10 Tage)"
                    use_existing=true
                fi
            else
                log "   ✅ Backup ist aktuell (< $DB_BACKUP_MAX_AGE_DAYS Tage)"
                use_existing=true
            fi
        else
            log "   ℹ️  Keine Immich-DB-Backups gefunden"
        fi
    fi
    
    # Nutze vorhandenes Backup oder erstelle neues
    if [ "$use_existing" = true ] && [ -n "$existing_backup" ]; then
        log "   📦 Kopiere vorhandenes Immich-Backup..."
        
        local dest_file="${BACKUP_DIR}/database/$(basename "$existing_backup")"
        
        if cp "$existing_backup" "$dest_file"; then
            log "   ✅ Backup kopiert"
            
            # Erstelle Symlink für einfacheren Zugriff
            ln -sf "$(basename "$dest_file")" "${BACKUP_DIR}/database/latest-db-backup.sql.gz"
            
            log "✅ Datenbank-Backup abgeschlossen (aus Immich-Backup)"
            log "   📊 Größe: $size"
            log "   📅 Stand: $backup_date"
        else
            log "   ❌ Fehler beim Kopieren, erstelle neues pg_dump..."
            use_existing=false
        fi
    fi
    
    # Falls kein vorhandenes Backup genutzt werden kann: pg_dump
    if [ "$use_existing" = false ]; then
        log "   🔄 Erstelle neues Datenbank-Backup via pg_dump..."
        
        export PGPASSWORD="$DB_PASSWORD"
        
        local db_file="${BACKUP_DIR}/database/immich_db_${TIMESTAMP}.sql"
        
        if ! pg_dump -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USERNAME" -d "$DB_NAME" \
            --no-owner --no-privileges --clean --if-exists \
            > "$db_file" 2>"${BACKUP_DIR}/logs/pg_dump.log"; then
            error_exit "Datenbank-Backup fehlgeschlagen (siehe ${BACKUP_DIR}/logs/pg_dump.log)"
        fi
        
        # Komprimiere Dump
        log "   🗜️  Komprimiere Datenbank-Backup..."
        gzip -9 "$db_file" || log "⚠️  Warnung: Komprimierung fehlgeschlagen"
        
        local db_size=$(du -sh "${db_file}.gz" | cut -f1)
        local line_count=$(gunzip -c "${db_file}.gz" | wc -l)
        
        log "✅ Datenbank-Backup abgeschlossen (neues pg_dump)"
        log "   📊 Größe: $db_size"
        log "   📝 Zeilen: $line_count"
    fi
}

backup_library() {
    log "📸 Starte Backup der Bildbibliothek..."
    
    if [ ! -d "$IMMICH_LIBRARY_DIR" ]; then
        log "⚠️  Library-Verzeichnis nicht gefunden - überspringe"
        return
    fi
    
    local file_count=$(find "$IMMICH_LIBRARY_DIR" -type f | wc -l)
    log "   📁 Dateien gefunden: $file_count"
    
    # rsync mit Archive-Mode und Checksum
    log "   🔄 Synchronisiere Dateien (kann einige Zeit dauern)..."
    
    if ! rsync -ah --info=progress2 --delete --checksum \
        --exclude="Thumbs.db" \
        --exclude=".DS_Store" \
        "$IMMICH_LIBRARY_DIR/" \
        "${BACKUP_DIR}/library/" \
        2>&1 | tee -a "${BACKUP_DIR}/logs/rsync.log"; then
        error_exit "Library-Backup fehlgeschlagen (siehe ${BACKUP_DIR}/logs/rsync.log)"
    fi
    
    local lib_size=$(du -sh "${BACKUP_DIR}/library" | cut -f1)
    local backed_up_files=$(find "${BACKUP_DIR}/library" -type f | wc -l)
    
    log "✅ Bildbibliothek-Backup abgeschlossen"
    log "   📊 Größe: $lib_size"
    log "   📁 Dateien: $backed_up_files"
}

fetch_immich_metadata() {
    if [ -z "$IMMICH_URL" ] || [ -z "$IMMICH_KEY" ]; then
        log "ℹ️  Immich API nicht konfiguriert - überspringe Metadaten-Export"
        return
    fi
    
    log "📋 Exportiere Immich-Metadaten..."
    
    # Hole Server-Info
    if curl -sf -H "x-api-key: $IMMICH_KEY" \
        "${IMMICH_URL}/api/server-info/version" \
        > "${BACKUP_DIR}/metadata/immich_version.json" 2>/dev/null; then
        local version=$(grep -oP '"version":"\K[^"]+' "${BACKUP_DIR}/metadata/immich_version.json" 2>/dev/null || echo "unknown")
        log "   ✅ Immich-Version: $version"
    fi
    
    # Hole Statistiken
    if curl -sf -H "x-api-key: $IMMICH_KEY" \
        "${IMMICH_URL}/api/server-info/statistics" \
        > "${BACKUP_DIR}/metadata/immich_statistics.json" 2>/dev/null; then
        log "   ✅ Immich-Statistiken exportiert"
    fi
}

create_backup_manifest() {
    log "📝 Erstelle Backup-Manifest..."
    
    local manifest_file="${BACKUP_DIR}/metadata/backup_manifest.txt"
    
    # Finde DB-Backup-Datei
    local db_backup_file=$(find "${BACKUP_DIR}/database" -name "*.sql.gz" -type f | head -1)
    local db_backup_source="Unbekannt"
    
    if [ -n "$db_backup_file" ]; then
        if [[ "$(basename "$db_backup_file")" == immich-db-backup-* ]]; then
            db_backup_source="Immich-Backup (kopiert)"
            
            # Parse Datum aus Dateinamen
            local date_str=$(basename "$db_backup_file" | grep -oP '\d{8}T\d{6}')
            if [ -n "$date_str" ]; then
                local year=${date_str:0:4}
                local month=${date_str:4:2}
                local day=${date_str:6:2}
                local hour=${date_str:9:2}
                local minute=${date_str:11:2}
                db_backup_source="$db_backup_source - Stand: ${year}-${month}-${day} ${hour}:${minute}"
            fi
        else
            db_backup_source="pg_dump (neu erstellt)"
        fi
    fi
    
    cat > "$manifest_file" << EOF
================================================================================
IMMICH BACKUP MANIFEST
================================================================================

Backup Information:
-------------------
Timestamp:        $TIMESTAMP
Date:             $(date '+%Y-%m-%d %H:%M:%S %Z')
Container:        $(hostname)
Script Version:   2.0 (mit Immich-Backup-Integration)

Source Paths:
-------------
Library:          $IMMICH_LIBRARY_DIR
Immich Backups:   $IMMICH_DB_BACKUP_DIR
Database Host:    $DB_HOST:$DB_PORT
Database Name:    $DB_NAME

Backup Location:
----------------
Target:           $BACKUP_TARGET
This Backup:      $BACKUP_DIR

Content Summary:
----------------
Database Backup:  $db_backup_source
EOF
    
    # Füge Größenangaben hinzu
    if [ -d "${BACKUP_DIR}/database" ]; then
        echo "Database Size:    $(du -sh "${BACKUP_DIR}/database" | cut -f1)" >> "$manifest_file"
    fi
    
    if [ -d "${BACKUP_DIR}/library" ]; then
        local lib_files=$(find "${BACKUP_DIR}/library" -type f | wc -l)
        echo "Library Size:     $(du -sh "${BACKUP_DIR}/library" | cut -f1)" >> "$manifest_file"
        echo "Library Files:    $lib_files" >> "$manifest_file"
    fi
    
    echo "Total Size:       $(du -sh "$BACKUP_DIR" | cut -f1)" >> "$manifest_file"
    
    echo "" >> "$manifest_file"
    echo "File Checksums:" >> "$manifest_file"
    echo "---------------" >> "$manifest_file"
    
    # Erstelle Checksummen für DB-Backup
    if [ -n "$db_backup_file" ] && [ -f "$db_backup_file" ]; then
        md5sum "$db_backup_file" | sed 's|.*/|DB: |' >> "$manifest_file"
    fi
    
    log "✅ Backup-Manifest erstellt"
}

create_restore_instructions() {
    local instructions_file="${BACKUP_DIR}/RESTORE_INSTRUCTIONS.txt"
    
    cat > "$instructions_file" << 'EOF'
================================================================================
IMMICH RESTORE ANLEITUNG
================================================================================

WICHTIG: Lesen Sie diese Anleitung vollständig vor der Wiederherstellung!

Voraussetzungen:
----------------
1. Immich-Installation muss existieren
2. Alle Container müssen gestoppt sein
3. PostgreSQL-Client muss verfügbar sein

Schritt 1: Container stoppen
-----------------------------
# Im LXC-Container
cd /pfad/zu/immich
docker-compose down

Schritt 2: Datenbank wiederherstellen
--------------------------------------
# Backup entpacken
gunzip -c database/*.sql.gz > /tmp/immich_restore.sql

# PostgreSQL-Container starten (nur DB)
docker-compose up -d immich_postgres

# Warte 10 Sekunden
sleep 10

# Datenbank droppen und neu erstellen
docker exec immich_postgres psql -U postgres -c "DROP DATABASE IF EXISTS immich;"
docker exec immich_postgres psql -U postgres -c "CREATE DATABASE immich;"

# Backup einspielen
docker exec -i immich_postgres psql -U postgres -d immich < /tmp/immich_restore.sql

# Temp-Datei löschen
rm /tmp/immich_restore.sql

Schritt 3: Library wiederherstellen
------------------------------------
# Aktuelles Library-Verzeichnis sichern (auf Proxmox-Host!)
mv /mnt/immich-library/library /mnt/immich-library/library.backup.$(date +%Y%m%d)

# Backup wiederherstellen
mkdir -p /mnt/immich-library/library
rsync -avh library/ /mnt/immich-library/library/

# Rechte setzen (falls nötig)
chown -R 1000:1000 /mnt/immich-library/library

Schritt 4: Container starten und neu scannen
---------------------------------------------
# Alle Container starten
docker-compose up -d

# Warte bis Immich bereit ist (~30 Sekunden)
sleep 30

# In Immich Web-UI einloggen und:
# Administration -> Jobs -> Library -> Scan All Libraries

Schritt 5: Verifikation
------------------------
# Prüfe ob alle Bilder da sind
# Prüfe ob Alben vorhanden sind
# Prüfe ob Personen erkannt wurden

================================================================================
NOTFALL-KONTAKTE:
- Immich Discord: https://discord.gg/immich
- Immich Docs: https://immich.app/docs
================================================================================
EOF
    
    log "✅ Wiederherstellungsanleitung erstellt"
}

cleanup_old_backups() {
    log "🧹 Bereinige alte Backups..."
    
    cd "$BACKUP_TARGET" || return
    
    # Zähle vorhandene Backups
    local backup_count=$(find . -maxdepth 1 -type d -name '[0-9]*' | wc -l)
    
    if [ "$backup_count" -le "$KEEP_BACKUPS" ]; then
        log "ℹ️  Keine alten Backups zu löschen (Anzahl: $backup_count, Behalte: $KEEP_BACKUPS)"
        return
    fi
    
    # Lösche älteste Backups
    local deleted=0
    find . -maxdepth 1 -type d -name '[0-9]*' -printf '%T@ %p\n' | \
        sort -n | head -n -"$KEEP_BACKUPS" | cut -d' ' -f2- | while read -r old_backup; do
        log "   🗑️  Lösche: $(basename "$old_backup")"
        rm -rf "$old_backup"
        ((deleted++)) || true
    done
    
    log "✅ Alte Backups bereinigt"
}

verify_backup() {
    log "🔍 Verifiziere Backup-Integrität..."
    
    local errors=0
    
    # Prüfe Verzeichnisstruktur
    if [ ! -d "$BACKUP_DIR" ]; then
        log "❌ Backup-Verzeichnis nicht gefunden"
        ((errors++))
    fi
    
    # Prüfe Datenbank-Backup
    local db_backup=$(find "${BACKUP_DIR}/database" -name "*.sql.gz" -type f | head -1)
    if [ -z "$db_backup" ]; then
        log "❌ Datenbank-Backup-Datei fehlt"
        ((errors++))
    else
        # Teste ob Datei entpackbar ist
        if ! gunzip -t "$db_backup" 2>/dev/null; then
            log "❌ Datenbank-Backup ist korrupt"
            ((errors++))
        else
            log "   ✅ Datenbank-Backup ist valide"
        fi
    fi
    
    # Prüfe Library-Backup
    if [ -d "${BACKUP_DIR}/library" ]; then
        local file_count=$(find "${BACKUP_DIR}/library" -type f | wc -l)
        if [ "$file_count" -eq 0 ]; then
            log "⚠️  Warnung: Library-Backup ist leer"
        else
            log "   ✅ Library-Backup enthält $file_count Dateien"
        fi
    else
        log "⚠️  Warnung: Library-Verzeichnis nicht gefunden"
    fi
    
    # Prüfe Manifest
    if [ ! -f "${BACKUP_DIR}/metadata/backup_manifest.txt" ]; then
        log "⚠️  Warnung: Backup-Manifest fehlt"
    fi
    
    if [ $errors -gt 0 ]; then
        error_exit "Backup-Verifikation fehlgeschlagen ($errors Fehler)"
    fi
    
    log "✅ Backup-Verifikation erfolgreich"
}

create_latest_symlink() {
    local latest_link="${BACKUP_TARGET}/latest"
    
    rm -f "$latest_link"
    cd "$BACKUP_TARGET"
    ln -sfn "$(basename "$BACKUP_DIR")" latest
    
    log "✅ Symlink 'latest' → $(basename "$BACKUP_DIR")"
}

print_summary() {
    local total_size=$(du -sh "$BACKUP_DIR" | cut -f1)
    local duration=$(($(date +%s) - START_TIME))
    local duration_min=$((duration / 60))
    local duration_sec=$((duration % 60))
    
    log ""
    log "════════════════════════════════════════════════════════════════"
    log "✅ BACKUP ERFOLGREICH ABGESCHLOSSEN"
    log "════════════════════════════════════════════════════════════════"
    log ""
    log "📊 Zusammenfassung:"
    log "   Gesamtgröße:    $total_size"
    log "   Dauer:          ${duration_min}m ${duration_sec}s"
    log "   Speicherort:    $BACKUP_DIR"
    log "   Symlink:        ${BACKUP_TARGET}/latest"
    log ""
    log "📋 Nächste Schritte:"
    log "   • Wiederherstellung: Siehe ${BACKUP_DIR}/RESTORE_INSTRUCTIONS.txt"
    log "   • Logs:             ${BACKUP_DIR}/logs/"
    log "   • Manifest:         ${BACKUP_DIR}/metadata/backup_manifest.txt"
    log ""
    log "════════════════════════════════════════════════════════════════"
}

# =============================================================================
# HAUPTPROGRAMM
# =============================================================================

main() {
    START_TIME=$(date +%s)
    
    log ""
    log "════════════════════════════════════════════════════════════════"
    log "🚀 IMMICH BACKUP GESTARTET"
    log "════════════════════════════════════════════════════════════════"
    log ""
    
    # Vorprüfungen
    check_prerequisites
    check_disk_space
    test_database_connection || true  # Nicht abbrechen wenn DB nicht erreichbar
    
    # Backup durchführen
    create_backup_structure
    backup_database
    backup_library
    fetch_immich_metadata
    
    # Metadaten und Dokumentation
    create_backup_manifest
    create_restore_instructions
    
    # Verifizierung
    verify_backup
    
    # Aufräumen
    cleanup_old_backups
    create_latest_symlink
    
    # Zusammenfassung
    print_summary
}

# Trap für Fehlerbehandlung
trap 'error_exit "Script wurde durch Signal unterbrochen"' INT TERM

# Script ausführen
main

exit 0
