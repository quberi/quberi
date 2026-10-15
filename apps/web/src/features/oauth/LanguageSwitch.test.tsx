import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { LanguageSwitch } from './LanguageSwitch'

const changeLanguage = vi.fn()
let language = 'en-US'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ i18n: { resolvedLanguage: language, changeLanguage } }),
}))

beforeEach(() => {
  changeLanguage.mockReset()
  window.localStorage.clear()
  language = 'en-US'
})

describe('LanguageSwitch', () => {
  it('offers Chinese while the page is in English, and remembers the choice', () => {
    render(<LanguageSwitch />)
    fireEvent.click(screen.getByRole('button', { name: /中文/ }))
    expect(changeLanguage).toHaveBeenCalledWith('zh-CN')
    // The same key the rest of the app reads on its next load.
    expect(window.localStorage.getItem('quberi.locale')).toBe('zh-CN')
  })

  it('offers English while the page is in Chinese', () => {
    language = 'zh-CN'
    render(<LanguageSwitch />)
    fireEvent.click(screen.getByRole('button', { name: /English/ }))
    expect(changeLanguage).toHaveBeenCalledWith('en-US')
    expect(window.localStorage.getItem('quberi.locale')).toBe('en-US')
  })

  it('still switches this visit if the browser will not store the choice', () => {
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage blocked')
    })
    render(<LanguageSwitch />)
    expect(() => fireEvent.click(screen.getByRole('button'))).not.toThrow()
    expect(changeLanguage).toHaveBeenCalledWith('zh-CN')
    set.mockRestore()
  })

  it('treats any other language as English', () => {
    language = 'fr'
    render(<LanguageSwitch />)
    expect(screen.getByRole('button', { name: /中文/ })).toBeInTheDocument()
  })
})
