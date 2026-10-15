import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { AdminApp } from './AdminApp'
import './admin.css'

const root = document.getElementById('admin-root')

if (!root) {
  throw new Error('Admin root element was not found')
}

createRoot(root).render(
  <StrictMode>
    <AdminApp />
  </StrictMode>,
)
