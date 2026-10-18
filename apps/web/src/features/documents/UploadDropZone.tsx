import { CloudUpload, Loader2 } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { useDocuments } from './DocumentsContext'
import { legacyOffice } from './legacyOffice'
import type { UploadHandle } from './DocumentsContext'

/**
 * Drop-zone + click-to-pick. Kicks off a per-file upload through the
 * DocumentsContext and shows an in-panel progress list.
 *
 * Deliberately no external drop libraries — the native drag events give
 * us everything we need for the MVP.
 */
export function UploadDropZone() {
  const { t } = useTranslation()
  const { upload, uploads } = useDocuments()
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragActive, setDragActive] = useState(false)
  const [refused, setRefused] = useState<string[]>([])
  const onFiles = useCallback(
    (files: FileList | null) => {
      if (!files || files.length === 0) return
      const turnedAway: string[] = []
      for (let i = 0; i < files.length; i++) {
        const f = files.item(i)
        if (!f) continue
        const old = legacyOffice(f.name)
        if (old) {
          turnedAway.push(
            t('documents.upload.legacy', {
              name: f.name,
              ext: old.ext,
              product: old.product,
              modern: old.modern,
            }),
          )
          continue
        }
        void upload(f)
      }
      setRefused(turnedAway)
    },
    [upload, t],
  )

  // Reset the native input after each pick so the *same file* can be
  // uploaded twice in a row.
  useEffect(() => {
    if (inputRef.current) inputRef.current.value = ''
  }, [uploads.length])

  return (
    <div className="documents-upload">
      <div
        className={`upload-dropzone ${dragActive ? 'active' : ''}`}
        onClick={() => inputRef.current?.click()}
        onDragEnter={(e) => {
          e.preventDefault()
          setDragActive(true)
        }}
        onDragLeave={(e) => {
          e.preventDefault()
          setDragActive(false)
        }}
        onDragOver={(e) => {
          e.preventDefault()
          setDragActive(true)
        }}
        onDrop={(e) => {
          e.preventDefault()
          setDragActive(false)
          onFiles(e.dataTransfer.files)
        }}
        role="button"
        tabIndex={0}
      >
        <CloudUpload size={22} />
        <div className="upload-dropzone-copy">
          <strong>{t('documents.upload.title')}</strong>
          <small>{t('documents.upload.hint')}</small>
        </div>
        <input
          accept=".txt,.md,.pdf,.docx,.xlsx,.pptx,.csv,.json,image/*,application/pdf"
          multiple
          onChange={(e) => onFiles(e.target.files)}
          ref={inputRef}
          style={{ display: 'none' }}
          type="file"
        />
      </div>

      {refused.length > 0 && (
        <ul className="upload-list upload-refused" role="alert">
          {refused.map((message) => (
            <li className="dialog-error" key={message}>
              {message}
            </li>
          ))}
        </ul>
      )}
      {uploads.length > 0 && (
        <ul className="upload-list">
          {uploads.map((u) => (
            <UploadRow handle={u} key={u.id} />
          ))}
        </ul>
      )}
    </div>
  )
}

function UploadRow({ handle }: { handle: UploadHandle }) {
  const { t } = useTranslation()
  const pct =
    handle.total > 0 ? Math.round((handle.loaded / handle.total) * 100) : 0
  const label =
    handle.status === 'done'
      ? t('documents.upload.done')
      : handle.status === 'failed'
        ? handle.message || t('documents.upload.failed')
        : t('documents.upload.progressPct', { pct })
  return (
    <li className={`upload-row status-${handle.status}`}>
      <span className="upload-row-icon">
        {handle.status === 'uploading' ? (
          <Loader2 className="spin" size={14} />
        ) : (
          <CloudUpload size={14} />
        )}
      </span>
      <span className="upload-row-body">
        <strong>{handle.file.name}</strong>
        <small>{label}</small>
      </span>
      <span className="upload-row-progress">
        <span className="upload-row-bar">
          <span
            className="upload-row-fill"
            style={{ width: `${handle.status === 'done' ? 100 : pct}%` }}
          />
        </span>
      </span>
    </li>
  )
}
