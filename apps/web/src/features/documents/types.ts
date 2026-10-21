/**
 * Frontend contracts for documents + document sources.
 * In lock-step with:
 *   apps/api/src/quberi_api/api/v1/spaces/documents.py
 *   apps/api/src/quberi_api/api/v1/organizations/document_sources.py
 */

export type DocumentSourceKind = 'local' | 'feishu' | 'oss_aliyun' | 's3_aws'
export type CachingPolicy = 'mirror' | 'reference_only'
export type SourceStatus = 'active' | 'paused' | 'error' | 'archived'
export type DocumentStatus = 'uploaded' | 'processing' | 'failed' | 'archived'
export type SyncState =
  | 'local_only'
  | 'synced_fresh'
  | 'synced_stale'
  | 'synced_broken'

export interface DocumentSource {
  id: string
  organization_id: string
  kind: DocumentSourceKind
  name: string
  config: Record<string, unknown>
  caching_policy: CachingPolicy
  status: SourceStatus
  credentials_present: boolean
  last_synced_at: string | null
  created_at: string
  updated_at: string
}

export interface DocumentSourceList {
  items: DocumentSource[]
  total: number
}

export interface RegisterSourceInput {
  kind: 'feishu' | 'oss_aliyun' | 's3_aws'
  name: string
  config?: Record<string, unknown>
  credentials?: Record<string, unknown> | null
  caching_policy?: CachingPolicy
}

export interface ConnectorResult {
  ok: boolean
  message: string
  details: Record<string, unknown>
}

export type IndexingState =
  | 'not_indexed'
  | 'indexing'
  | 'indexed'
  | 'failed'

export interface DocumentSummary {
  id: string
  space_id: string
  source_id: string | null
  source_kind: DocumentSourceKind
  source_name: string
  name: string
  mime_type: string
  size_bytes: number
  checksum_sha256: string | null
  storage_key: string | null
  external_ref: Record<string, unknown>
  sync_state: SyncState
  status: DocumentStatus
  uploaded_by_user_id: string | null
  uploader_display_name: string
  created_at: string
  updated_at: string
  // RAG groundwork (Slice G): all fresh uploads land as
  // ``not_indexed`` today. The RAG slice will flip these.
  indexing_state: IndexingState
  chunk_count: number
  last_indexed_at: string | null
  indexing_error: string | null
  /** 'approved', or 'pending' / 'rejected' in a space that approves uploads. */
  review_state?: 'approved' | 'pending' | 'rejected'
  review_note?: string | null
  /** Guest sandbox only: uploaded from the requesting browser. */
  is_mine?: boolean
  /** Exception rules on this document. Present only for someone who manages the space. */
  allow_rules?: number | null
  deny_rules?: number | null
}

export interface DocumentListFilters {
  q: string | null
  source_kind: DocumentSourceKind | null
  file_type?: DocumentFileType | null
  order_by: 'created_at' | 'updated_at' | 'name' | 'size_bytes'
  order: 'asc' | 'desc'
  include_archived: boolean
}

export interface DocumentList {
  items: DocumentSummary[]
  total: number
  limit: number
  offset: number
  filters: DocumentListFilters
}

export interface DocumentStats {
  total: number
  total_size_bytes: number
  last_uploaded_at: string | null
  by_source: Record<string, number>
  by_mime_group: Record<string, number>
  by_indexing_state: Record<string, number>
}

/** The file-type groups a list can be narrowed to (same names as ``by_mime_group``). */
export type DocumentFileType =
  | 'image'
  | 'pdf'
  | 'word'
  | 'spreadsheet'
  | 'presentation'
  | 'text'
  | 'other'

export interface ListDocumentsParams {
  q?: string
  source_kind?: DocumentSourceKind
  file_type?: DocumentFileType
  order_by?: 'created_at' | 'updated_at' | 'name' | 'size_bytes'
  order?: 'asc' | 'desc'
  include_archived?: boolean
  limit?: number
  offset?: number
}
