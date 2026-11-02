'use client'

import {
  useState,
  useRef,
  useEffect,
  useCallback,
  useLayoutEffect,
  type KeyboardEvent,
} from 'react'
import { useRouter } from 'next/navigation'
import Image from 'next/image'
import { useLocale, useTranslations } from 'next-intl'
import {
  ArrowDown,
  Check,
  Copy,
  Pencil,
  RefreshCw,
  Search,
  Square,
  Trash2,
  X,
} from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { useAuthStore } from '@/store/auth'
import { useLanguageStore } from '@/store/language'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { AudioPlayer } from '@/components/ui/AudioPlayer'
import { VoiceRecorder } from '@/components/ui/VoiceRecorder'
import { MaintenanceGate } from '@/components/billing/MaintenanceBanner'
import { PaywallBanner } from '@/components/billing/PaywallBanner'
import { FreemiumQuotaBanner } from '@/components/billing/FreemiumQuotaBanner'
import { useFreemiumStore } from '@/store/freemium'
import { useConfigStore } from '@/store/config'
import { isSubscribed, isFreemiumTrialActive } from '@/store/auth'
import { WordTooltip, useWordSave } from '@/components/ui/WordTooltip'
import { PageLoading } from '@/components/ui/page-loading'
import { TargetLanguageText } from '@/components/TargetLanguageText'
import { ChatMarkdown } from '@/components/chat/ChatMarkdown'
import { AuthAvatarImage } from '@/components/AuthAvatarImage'
import { MemorySavedToast } from '@/components/memory/MemorySavedToast'
import { useTransientToast } from '@/hooks/useTransientToast'
import { readSseData } from '@/lib/sse'
import LinguAvatar from '@/components/lingu/LinguAvatar'

interface Message {
  id?: number
  role: 'user' | 'assistant'
  content: string
  created_at?: string
  modality?: string | null
  stopped?: boolean
}

interface Conversation {
  id: number
  title: string
  source: string
  created_at: string
  updated_at: string
  ai_channel_id?: number | null
}

interface ChatOptions {
  can_switch_model: boolean
  models: { id: number; name: string; model: string }[]
  study_plan_id: number | null
}

interface ChatSseEvent {
  conversation_id?: number
  user_message_id?: number
  message_id?: number
  token?: string
  response_reset?: boolean
  error?: string
  done?: boolean
  memory_updated?: boolean
}

type SendMode =
  | { kind: 'new'; text: string }
  | { kind: 'edit'; index: number; id: number; text: string }
  | { kind: 'regenerate' }

type Group = 'today' | 'yesterday' | 'week' | 'month' | 'older'
const GROUPS: Group[] = ['today', 'yesterday', 'week', 'month', 'older']
const MAX_INPUT_HEIGHT = 200

// The backend stores naive UTC timestamps.
function parseServerDate(value: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
}

function groupOf(value: string, now: Date): Group {
  const startOfToday = new Date(now)
  startOfToday.setHours(0, 0, 0, 0)
  const days = (startOfToday.getTime() - parseServerDate(value).getTime()) / 86_400_000
  if (days <= 0) return 'today'
  if (days <= 1) return 'yesterday'
  if (days <= 7) return 'week'
  if (days <= 30) return 'month'
  return 'older'
}

function isChatTurn(message: Message) {
  return !message.modality || message.modality === 'chat'
}

export default function ChatPage() {
  const t = useTranslations('chat')
  const tCommon = useTranslations('common')
  const tLang = useTranslations('targetLanguages')
  const tQuota = useTranslations('conversation')
  const locale = useLocale()
  const router = useRouter()
  const user = useAuthStore((s) => s.user)
  const sessionVersion = useAuthStore((s) => s.sessionVersion)
  const activeLanguage = useLanguageStore((s) => s.activeLanguage)
  const {
    selectedWord,
    tooltipPos,
    saveState,
    handleTextSelection,
    handleSaveWord,
    dismissTooltip,
  } = useWordSave()
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [sendingWarn, setSendingWarn] = useState(false)
  const sendingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [error, setError] = useState('')
  const [loadingConvs, setLoadingConvs] = useState(true)
  const [convLoadError, setConvLoadError] = useState(false)
  const [loadingMsgs, setLoadingMsgs] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [deletePending, setDeletePending] = useState<number | null>(null)
  const [search, setSearch] = useState('')
  const [renamingId, setRenamingId] = useState<number | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [editValue, setEditValue] = useState('')
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null)
  const [atBottom, setAtBottom] = useState(true)
  const [options, setOptions] = useState<ChatOptions | null>(null)
  const [draftChannelId, setDraftChannelId] = useState<number | null>(null)
  const [voiceBusy, setVoiceBusy] = useState(false)
  const {
    visible: memoryToast,
    announcementId: memoryToastId,
    show: showMemoryToast,
  } = useTransientToast()
  const scrollRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const searchRef = useRef('')
  const targetLanguageCode = activeLanguage?.code ?? 'en-GB'

  const stripeEnabled = useConfigStore((s) => s.stripeEnabled)
  const freemiumStatus = useFreemiumStore((s) => s.status)
  const fetchFreemium = useFreemiumStore((s) => s.fetchStatus)
  const decrementFreemium = useFreemiumStore((s) => s.decrement)
  const freemiumExhausted =
    stripeEnabled &&
    !isSubscribed(user, stripeEnabled) &&
    !isFreemiumTrialActive(user, stripeEnabled) &&
    freemiumStatus &&
    freemiumStatus.chat_remaining <= 0

  useEffect(() => {
    if (stripeEnabled && !isSubscribed(user, stripeEnabled)) {
      fetchFreemium()
    }
  }, [stripeEnabled, user, fetchFreemium])

  const scrollBottom = useCallback((smooth = true) => {
    bottomRef.current?.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto' })
  }, [])

  // Follow new output only while the reader is already at the bottom.
  useEffect(() => {
    if (atBottom) scrollBottom()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, scrollBottom])

  function handleScroll() {
    const el = scrollRef.current
    if (!el) return
    setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight < 80)
  }

  // Grow the input with its content, up to a limit.
  useLayoutEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_INPUT_HEIGHT)}px`
  }, [input])

  // Open sidebar by default only on desktop
  useEffect(() => {
    setSidebarOpen(window.innerWidth >= 768)
  }, [])

  // Warn if LLM takes longer than 60 s
  useEffect(() => {
    if (sending) {
      setSendingWarn(false)
      sendingTimerRef.current = setTimeout(() => setSendingWarn(true), 60_000)
    } else {
      if (sendingTimerRef.current) clearTimeout(sendingTimerRef.current)
      setSendingWarn(false)
    }
    return () => {
      if (sendingTimerRef.current) clearTimeout(sendingTimerRef.current)
    }
  }, [sending])

  // Stop any reply in progress when leaving the page.
  useEffect(() => () => abortRef.current?.abort(), [])

  const loadConversations = useCallback(async (query?: string) => {
    const q = (query ?? searchRef.current).trim()
    try {
      const res = await apiFetch(
        q
          ? `/api/chat/conversations?q=${encodeURIComponent(q)}`
          : '/api/chat/conversations'
      )
      if (res.ok) {
        const data: Conversation[] = await res.json()
        setConversations(data)
        return data
      }
    } catch {
      /* ignore */
    }
    return null
  }, [])

  // Load conversations and chat options on mount, auto-select the most recent
  useEffect(() => {
    async function init() {
      setConvLoadError(false)
      setLoadingConvs(true)
      setSearch('')
      searchRef.current = ''
      apiFetch('/api/chat/options')
        .then((res) => (res.ok ? res.json() : null))
        .then((data: ChatOptions | null) => setOptions(data))
        .catch(() => setOptions(null))
      const data = await loadConversations('')
      if (data === null) {
        setConvLoadError(true)
        setLoadingConvs(false)
        return
      }
      if (data.length > 0) {
        selectConversation(data[0].id)
      } else {
        setActiveId(null)
        setMessages([])
      }
      setLoadingConvs(false)
    }
    init()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeLanguage?.code])

  // Debounced server-side search over titles and messages.
  useEffect(() => {
    if (searchRef.current === search) return
    searchRef.current = search
    const timer = setTimeout(() => {
      loadConversations(search)
    }, 300)
    return () => clearTimeout(timer)
  }, [search, loadConversations])

  async function selectConversation(id: number) {
    if (sending) return
    dismissTooltip()
    if (window.innerWidth < 768) setSidebarOpen(false)
    setActiveId(id)
    setMessages([])
    setError('')
    setEditingIndex(null)
    setLoadingMsgs(true)
    setAtBottom(true)
    try {
      const res = await apiFetch(`/api/chat/conversations/${id}/messages`)
      if (res.ok) {
        const data = await res.json()
        setMessages(data.messages || [])
        requestAnimationFrame(() => scrollBottom(false))
      }
    } catch {
      /* ignore */
    } finally {
      setLoadingMsgs(false)
    }
  }

  function newChat() {
    if (sending) return
    // Don't create — let the first message auto-create the conversation
    dismissTooltip()
    setActiveId(null)
    setMessages([])
    setError('')
    setEditingIndex(null)
    setDraftChannelId(null)
    if (window.innerWidth < 768) setSidebarOpen(false)
    requestAnimationFrame(() => inputRef.current?.focus())
  }

  function continueInVoice() {
    const context = messages
      .filter((m) => m.content.trim().length > 0)
      .map(({ role, content }) => ({ role, content }))
      .slice(-20)
    // Only pass the message context — no conversation_id.
    // The voice session will create its own new conversation record so the
    // original text chat stays clean and the two appear as separate entries
    // in the sidebar (the voice one gets the 🎤 icon).
    sessionStorage.setItem(
      'voice_context',
      JSON.stringify({
        messages: context,
      })
    )
    router.push('/conversation')
  }

  async function deleteConversation(id: number) {
    await apiFetch(`/api/chat/conversations/${id}`, { method: 'DELETE' })
    setDeletePending(null)
    const updated = await loadConversations()
    if (updated === null) {
      setConvLoadError(true)
      return
    }
    if (activeId === id) {
      if (updated.length > 0) {
        selectConversation(updated[0].id)
      } else {
        setActiveId(null)
        setMessages([])
      }
    }
  }

  async function patchConversation(
    id: number,
    body: { title?: string; ai_channel_id?: number | null }
  ) {
    try {
      const res = await apiFetch(`/api/chat/conversations/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) return false
      const updated: Conversation = await res.json()
      setConversations((list) =>
        list.map((c) => (c.id === id ? { ...c, ...updated } : c))
      )
      return true
    } catch {
      return false
    }
  }

  async function saveRename() {
    if (renamingId === null) return
    const title = renameValue.trim()
    const id = renamingId
    setRenamingId(null)
    if (title) await patchConversation(id, { title })
  }

  async function generateTitle(id: number) {
    try {
      const res = await apiFetch(`/api/chat/conversations/${id}/title`, {
        method: 'POST',
      })
      if (!res.ok) return
      const data: { title: string } = await res.json()
      setConversations((list) =>
        list.map((c) => (c.id === id ? { ...c, title: data.title } : c))
      )
    } catch {
      /* keep the provisional title */
    }
  }

  async function changeModel(value: string) {
    const channelId = value ? Number(value) : null
    if (activeId === null) {
      setDraftChannelId(channelId)
      return
    }
    await patchConversation(activeId, { ai_channel_id: channelId })
  }

  async function copyMessage(index: number, content: string) {
    try {
      await navigator.clipboard.writeText(content)
      setCopiedIndex(index)
      setTimeout(
        () => setCopiedIndex((current) => (current === index ? null : current)),
        1500
      )
    } catch {
      /* clipboard unavailable */
    }
  }

  function stopReply() {
    abortRef.current?.abort()
  }

  function updateLast(patch: Partial<Message>) {
    setMessages((prev) => {
      const copy = [...prev]
      copy[copy.length - 1] = { ...copy[copy.length - 1], ...patch }
      return copy
    })
  }

  async function streamChat(mode: SendMode) {
    if (sending) return
    const startedWithoutConversation = activeId === null
    setError('')
    setEditingIndex(null)
    setAtBottom(true)

    const now = new Date().toISOString()
    let body: Record<string, unknown>
    if (mode.kind === 'new') {
      body = { message: mode.text, conversation_id: activeId }
      if (activeId === null && draftChannelId !== null) {
        body.ai_channel_id = draftChannelId
      }
      setMessages((prev) => [
        ...prev,
        { role: 'user', content: mode.text, created_at: now },
        { role: 'assistant', content: '' },
      ])
    } else if (mode.kind === 'edit') {
      body = {
        message: mode.text,
        conversation_id: activeId,
        edit_message_id: mode.id,
      }
      setMessages((prev) => [
        ...prev.slice(0, mode.index),
        { role: 'user', content: mode.text, created_at: now },
        { role: 'assistant', content: '' },
      ])
    } else {
      body = { conversation_id: activeId, regenerate: true }
      setMessages((prev) => [
        ...prev.slice(0, prev.length - 1),
        { role: 'assistant', content: '' },
      ])
    }

    const controller = new AbortController()
    abortRef.current = controller
    setSending(true)
    let assistantContent = ''
    let conversationId = activeId

    try {
      const res = await apiFetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      })

      if (!res.ok) {
        let message = t('errorMessage')
        if (res.status === 429) {
          const data = await res.json().catch(() => ({}))
          if (
            typeof data.detail === 'string' &&
            data.detail.startsWith('Monthly token limit reached')
          ) {
            message = tQuota('quotaExceededTokens')
          }
        }
        setMessages((prev) => prev.slice(0, -1))
        setError(message)
        return
      }

      let streamCompleted = false
      if (!res.body) throw new Error(t('errorMessage'))
      for await (const data of readSseData<ChatSseEvent>(res.body)) {
        if (data.conversation_id) {
          conversationId = data.conversation_id
          if (startedWithoutConversation) {
            setActiveId(data.conversation_id)
            loadConversations()
          }
        }
        if (data.user_message_id) {
          const userId = data.user_message_id
          setMessages((prev) => {
            const copy = [...prev]
            const index = copy.length - 2
            if (copy[index]?.role === 'user') {
              copy[index] = { ...copy[index], id: userId }
            }
            return copy
          })
        }
        if (data.response_reset) {
          dismissTooltip()
          assistantContent = ''
          updateLast({ content: '' })
        }
        if (data.token) {
          assistantContent += data.token
          updateLast({ content: assistantContent })
        }
        if (data.error) {
          streamCompleted = true
          if (!assistantContent) setMessages((prev) => prev.slice(0, -1))
          setError(t('errorMessage'))
        }
        if (data.done) {
          streamCompleted = true
          updateLast({
            id: data.message_id,
            created_at: new Date().toISOString(),
          })
          if (
            !isSubscribed(user, stripeEnabled) &&
            !isFreemiumTrialActive(user, stripeEnabled) &&
            useAuthStore.getState().sessionVersion === sessionVersion
          ) {
            decrementFreemium('chat_remaining')
          }
          if (startedWithoutConversation && conversationId !== null) {
            generateTitle(conversationId)
          }
          loadConversations()
        }
        if (data.memory_updated) showMemoryToast()
      }
      if (!streamCompleted) throw new Error(t('errorMessage'))
    } catch {
      if (controller.signal.aborted) {
        // The server keeps the partial reply; mirror that locally.
        if (assistantContent.trim()) {
          updateLast({ stopped: true, created_at: new Date().toISOString() })
        } else {
          setMessages((prev) => prev.slice(0, -1))
        }
        loadConversations()
      } else {
        if (!assistantContent) {
          setMessages((prev) =>
            prev[prev.length - 1]?.role === 'assistant' ? prev.slice(0, -1) : prev
          )
        }
        setError(t('errorMessage'))
      }
    } finally {
      abortRef.current = null
      setSending(false)
      inputRef.current?.focus()
    }
  }

  function sendMessage() {
    const text = input.trim()
    if (!text || sending) return
    setInput('')
    void streamChat({ kind: 'new', text })
  }

  function handleInputKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Ignore Enter while an input method (e.g. Chinese pinyin) is composing.
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      sendMessage()
    }
  }

  function formatTime(value?: string) {
    if (!value) return ''
    const date = parseServerDate(value)
    const sameDay = date.toDateString() === new Date().toDateString()
    return new Intl.DateTimeFormat(locale, {
      ...(sameDay ? {} : { month: 'short', day: 'numeric' }),
      hour: '2-digit',
      minute: '2-digit',
    }).format(date)
  }

  const activeConversation = conversations.find((c) => c.id === activeId)
  const lastUserIndex = messages.map((m) => m.role).lastIndexOf('user')
  const canRegenerate =
    !sending &&
    activeId !== null &&
    messages.length > 0 &&
    messages[messages.length - 1].role === 'assistant' &&
    lastUserIndex >= 0 &&
    messages[lastUserIndex].id !== undefined &&
    isChatTurn(messages[lastUserIndex])
  const selectedChannel =
    activeId === null ? draftChannelId : (activeConversation?.ai_channel_id ?? null)
  const showModelPicker =
    options?.can_switch_model === true && options.models.length > 0
  const now = new Date()
  const grouped = GROUPS.map((group) => ({
    group,
    items: conversations.filter((c) => groupOf(c.updated_at, now) === group),
  })).filter((section) => section.items.length > 0)

  const iconButton =
    'text-fl-muted-3 hover:text-fl-fg inline-flex items-center gap-1 px-1.5 py-1 font-mono text-[11px] tracking-wide transition-colors disabled:opacity-40'

  return (
    <MaintenanceGate>
      <div className="flex h-[calc(100dvh-56px)] w-full overflow-hidden md:h-screen">
        <MemorySavedToast
          visible={memoryToast}
          announcementId={memoryToastId}
        />
        {/* Sidebar backdrop — mobile only */}
        {sidebarOpen && (
          <div
            className="fixed inset-x-0 top-14 bottom-0 z-10 bg-black/40 md:hidden"
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* Sidebar */}
        {sidebarOpen && (
          <aside className="border-fl-border bg-fl-bg fixed top-14 bottom-0 left-0 z-20 flex w-64 shrink-0 flex-col overflow-hidden border-r md:relative md:top-auto md:bottom-auto md:left-auto md:z-auto">
            <div className="border-fl-border space-y-3 border-b px-3 py-3">
              <button
                onClick={newChat}
                disabled={sending}
                className="border-fl-border text-fl-fg hover:bg-fl-surface w-full border px-3 py-2 text-left font-mono text-xs tracking-widest uppercase transition-colors disabled:opacity-40"
                title={t('newConversation')}
              >
                + {t('newConversation')}
              </button>
              <label className="border-fl-border bg-fl-surface flex items-center gap-2 border px-2 py-1.5">
                <Search
                  className="text-fl-muted-3 size-3.5 shrink-0"
                  aria-hidden="true"
                />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={t('searchPlaceholder')}
                  aria-label={t('searchPlaceholder')}
                  maxLength={100}
                  className="text-fl-fg placeholder:text-fl-muted-4 min-w-0 flex-1 bg-transparent font-mono text-xs focus:outline-none"
                />
                {search && (
                  <button
                    onClick={() => setSearch('')}
                    className="text-fl-muted-3 hover:text-fl-fg"
                    aria-label={t('clearSearch')}
                  >
                    <X className="size-3.5" aria-hidden="true" />
                  </button>
                )}
              </label>
            </div>
            <div className="flex-1 overflow-y-auto">
              {loadingConvs ? (
                <PageLoading fullScreen={false} className="block px-4 py-4" />
              ) : convLoadError ? (
                <div className="flex flex-col items-center gap-3 px-4 py-6">
                  <p className="text-fl-error font-mono text-xs">
                    {tCommon('error')}
                  </p>
                  <button
                    onClick={() => {
                      setConvLoadError(false)
                      setLoadingConvs(true)
                      loadConversations()
                        .then((data) => {
                          if (data === null) {
                            setConvLoadError(true)
                          } else if (data.length > 0) {
                            selectConversation(data[0].id)
                          }
                        })
                        .finally(() => setLoadingConvs(false))
                    }}
                    className="border-fl-border text-fl-label text-fl-muted-1 hover:text-fl-fg hover:border-fl-border-2 border px-4 py-2 font-mono tracking-widest uppercase transition-colors"
                  >
                    {tCommon('retry')}
                  </button>
                </div>
              ) : conversations.length === 0 ? (
                <p className="text-fl-label text-fl-muted-4 px-4 py-4 font-mono">
                  {search.trim() ? t('noResults') : t('noConversation')}
                </p>
              ) : (
                grouped.map(({ group, items }) => (
                  <div key={group}>
                    <p className="text-fl-hint text-fl-muted-4 px-4 pt-4 pb-1 font-mono tracking-widest uppercase">
                      {t(`group.${group}`)}
                    </p>
                    {items.map((c) =>
                      renamingId === c.id ? (
                        <div
                          key={c.id}
                          className="border-fl-surface-2 flex items-center gap-1 border-b px-3 py-2"
                        >
                          <input
                            autoFocus
                            value={renameValue}
                            maxLength={200}
                            onChange={(e) => setRenameValue(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === 'Enter' && !e.nativeEvent.isComposing)
                                saveRename()
                              if (e.key === 'Escape') setRenamingId(null)
                            }}
                            onBlur={saveRename}
                            aria-label={t('rename')}
                            className="border-fl-border-2 bg-fl-surface text-fl-fg min-w-0 flex-1 border px-2 py-1 font-mono text-xs focus:outline-none"
                          />
                        </div>
                      ) : (
                        <div
                          key={c.id}
                          onClick={() => selectConversation(c.id)}
                          className={`group border-fl-surface-2 flex cursor-pointer items-center justify-between gap-1 border-b px-4 py-2.5 transition-colors ${
                            activeId === c.id
                              ? 'bg-fl-surface-2 border-l-fl-fg border-l-2'
                              : 'hover:bg-fl-surface border-l-2 border-l-transparent'
                          }`}
                        >
                          <span
                            className={`text-fl-label truncate pr-1 font-mono leading-tight ${activeId === c.id ? 'text-fl-fg' : 'text-fl-muted-1'}`}
                            title={c.title}
                          >
                            {c.source === 'voice' && (
                              <span
                                className="text-fl-muted-3 mr-1.5"
                                title={t('voiceSession')}
                              >
                                🎤
                              </span>
                            )}
                            {c.title}
                          </span>
                          <span className="flex shrink-0 items-center opacity-100 transition-opacity md:opacity-0 md:group-hover:opacity-100">
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                setRenamingId(c.id)
                                setRenameValue(c.title)
                              }}
                              className="text-fl-muted-3 hover:text-fl-fg p-1"
                              title={t('rename')}
                              aria-label={t('rename')}
                            >
                              <Pencil className="size-3" aria-hidden="true" />
                            </button>
                            <button
                              onClick={(e) => {
                                e.stopPropagation()
                                setDeletePending(c.id)
                              }}
                              className="text-fl-error-fg hover:text-fl-error p-1"
                              title={t('deleteConfirm')}
                              aria-label={t('deleteConfirm')}
                            >
                              <Trash2 className="size-3" aria-hidden="true" />
                            </button>
                          </span>
                        </div>
                      )
                    )}
                  </div>
                ))
              )}
            </div>
          </aside>
        )}

        {/* Main chat area */}
        <div className="flex flex-1 flex-col overflow-hidden">
          {/* Header */}
          <div className="border-fl-border bg-fl-bg flex shrink-0 items-center gap-2 border-b px-5 py-3">
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="text-fl-label text-fl-muted-2 hover:text-fl-fg mr-1 text-lg transition-colors"
              title={
                sidebarOpen ? t('toggleSidebarHide') : t('toggleSidebarShow')
              }
            >
              {sidebarOpen ? '◀' : '☰'}
            </button>
            <LinguAvatar
              animation={sending ? 'pensando' : 'reposo'}
              className="!mx-0 h-[55px] w-[55px] shrink-0"
            />
            <div className="min-w-0 flex-1">
              <span className="text-fl-label text-fl-muted-2 block truncate font-mono tracking-widest uppercase">
                {activeId
                  ? (activeConversation?.title ?? t('title'))
                  : t('newConversation')}
              </span>
              {sending && (
                <span className="text-fl-hint text-fl-muted-3 animate-pulse font-mono tracking-widest uppercase">
                  {t('thinking')}
                  {sendingWarn && (
                    <span className="ml-2 text-amber-500">
                      {t('takingLonger')}
                    </span>
                  )}
                </span>
              )}
            </div>
            {showModelPicker && (
              <label className="flex shrink-0 items-center gap-1.5">
                <span className="text-fl-hint text-fl-muted-3 hidden font-mono tracking-widest uppercase sm:inline">
                  {t('modelLabel')}
                </span>
                <select
                  value={selectedChannel ?? ''}
                  onChange={(e) => changeModel(e.target.value)}
                  disabled={sending}
                  aria-label={t('modelLabel')}
                  className="border-fl-border bg-fl-surface text-fl-fg max-w-40 border px-2 py-1 font-mono text-xs disabled:opacity-50"
                >
                  <option value="">{t('modelAuto')}</option>
                  {options?.models.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.name} · {model.model}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {!sending && messages.length > 0 && (
              <button
                onClick={continueInVoice}
                className="text-fl-hint text-fl-muted-2 hover:text-fl-fg shrink-0 font-mono tracking-widest uppercase transition-colors"
              >
                {t('continueInVoice')}
              </button>
            )}
          </div>

          <FreemiumQuotaBanner feature="chat" />

          {/* Messages */}
          <div className="relative min-h-0 flex-1">
            <div
              ref={scrollRef}
              onScroll={handleScroll}
              className="h-full space-y-5 overflow-y-auto px-6 py-4"
            >
              {loadingMsgs ? (
                <div className="flex h-full items-center justify-center">
                  <PageLoading fullScreen={false} />
                </div>
              ) : messages.length === 0 ? (
                <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                  <p className="text-fl-label text-fl-muted-3 font-mono tracking-widest uppercase">
                    {t('title')}
                  </p>
                  <p className="text-fl-muted-2 max-w-xs font-mono text-xs leading-relaxed">
                    {t('subtitle', {
                      language: activeLanguage
                        ? tLang(activeLanguage.code)
                        : tLang('en-GB'),
                    })}
                  </p>
                </div>
              ) : (
                messages.map((msg, i) => {
                  const streaming = sending && i === messages.length - 1
                  const isUser = msg.role === 'user'
                  const editable =
                    isUser &&
                    !sending &&
                    msg.id !== undefined &&
                    isChatTurn(msg)
                  return (
                    <div
                      key={msg.id ?? `local-${i}`}
                      className={`group flex items-start gap-2 ${isUser ? 'ml-auto max-w-[85%] flex-row-reverse' : 'flex-row'}`}
                    >
                      {/* Avatar */}
                      <div className="border-fl-border mt-0.5 h-7 w-7 flex-shrink-0 overflow-hidden rounded-full border">
                        {!isUser ? (
                          <Image
                            src="/logo_head.png"
                            alt="Lingu"
                            width={28}
                            height={28}
                            className="h-full w-full object-cover"
                          />
                        ) : user?.avatar ? (
                          <AuthAvatarImage
                            avatar={user.avatar}
                            alt=""
                            width={28}
                            height={28}
                            className="h-full w-full object-cover"
                            fallback={
                              <div className="bg-fl-surface-2 flex h-full w-full items-center justify-center">
                                <span className="text-fl-hint text-fl-muted-1 font-mono select-none">
                                  {(user?.displayName ||
                                    user?.username ||
                                    '?')[0].toUpperCase()}
                                </span>
                              </div>
                            }
                          />
                        ) : (
                          <div className="bg-fl-surface-2 flex h-full w-full items-center justify-center">
                            <span className="text-fl-hint text-fl-muted-1 font-mono select-none">
                              {(user?.displayName ||
                                user?.username ||
                                '?')[0].toUpperCase()}
                            </span>
                          </div>
                        )}
                      </div>
                      <div
                        className={`flex max-w-[85%] min-w-[10rem] flex-col ${isUser ? 'items-end' : 'items-start'}`}
                      >
                        {editingIndex === i ? (
                          <div className="border-fl-border-2 bg-fl-surface w-full min-w-[16rem] border p-2">
                            <textarea
                              autoFocus
                              value={editValue}
                              onChange={(e) => setEditValue(e.target.value)}
                              onKeyDown={(e) => {
                                if (e.key === 'Escape') setEditingIndex(null)
                              }}
                              rows={Math.min(8, editValue.split('\n').length + 1)}
                              maxLength={5000}
                              aria-label={t('edit')}
                              className="text-fl-fg w-full resize-none bg-transparent font-sans text-base focus:outline-none"
                            />
                            <div className="mt-2 flex justify-end gap-2">
                              <button
                                onClick={() => setEditingIndex(null)}
                                className="border-fl-border text-fl-muted-1 hover:text-fl-fg border px-3 py-1 font-mono text-xs tracking-widest uppercase"
                              >
                                {t('editCancel')}
                              </button>
                              <button
                                onClick={() =>
                                  msg.id !== undefined &&
                                  editValue.trim() &&
                                  streamChat({
                                    kind: 'edit',
                                    index: i,
                                    id: msg.id,
                                    text: editValue.trim(),
                                  })
                                }
                                disabled={!editValue.trim()}
                                className="bg-fl-accent text-fl-accent-fg hover:bg-fl-accent/90 px-3 py-1 font-mono text-xs font-bold tracking-widest uppercase disabled:opacity-40"
                              >
                                {t('editSave')}
                              </button>
                            </div>
                          </div>
                        ) : (
                          <TargetLanguageText
                            as="div"
                            languageCode={targetLanguageCode}
                            className={`word-selectable max-w-[70ch] border px-4 py-3 text-left break-words ${
                              isUser
                                ? 'bg-fl-accent text-fl-accent-fg border-fl-accent whitespace-pre-wrap'
                                : 'bg-fl-surface text-fl-fg-2 border-fl-border'
                            }`}
                            onPointerUp={
                              !isUser && !streaming
                                ? () => handleTextSelection(msg.content)
                                : undefined
                            }
                          >
                            {msg.content ? (
                              !isUser ? (
                                <ChatMarkdown>{msg.content}</ChatMarkdown>
                              ) : (
                                msg.content
                              )
                            ) : streaming ? (
                              <span className="text-fl-muted-2 animate-pulse">
                                ▌
                              </span>
                            ) : null}
                          </TargetLanguageText>
                        )}

                        {/* Message actions */}
                        {editingIndex !== i && msg.content && !streaming && (
                          <div
                            className={`mt-1 flex flex-wrap items-center gap-0.5 ${isUser ? 'flex-row-reverse' : ''}`}
                          >
                            {!isUser && (
                              <AudioPlayer
                                text={msg.content}
                                conversationId={activeId ?? undefined}
                                size="sm"
                              />
                            )}
                            <button
                              onClick={() => copyMessage(i, msg.content)}
                              className={iconButton}
                              title={t('copy')}
                            >
                              {copiedIndex === i ? (
                                <Check className="size-3.5" aria-hidden="true" />
                              ) : (
                                <Copy className="size-3.5" aria-hidden="true" />
                              )}
                              <span className="sr-only sm:not-sr-only">
                                {copiedIndex === i ? t('copied') : t('copy')}
                              </span>
                            </button>
                            {editable && (
                              <button
                                onClick={() => {
                                  setEditingIndex(i)
                                  setEditValue(msg.content)
                                }}
                                className={iconButton}
                                title={t('edit')}
                              >
                                <Pencil className="size-3.5" aria-hidden="true" />
                                <span className="sr-only sm:not-sr-only">
                                  {t('edit')}
                                </span>
                              </button>
                            )}
                            {!isUser &&
                              i === messages.length - 1 &&
                              canRegenerate && (
                                <button
                                  onClick={() =>
                                    streamChat({ kind: 'regenerate' })
                                  }
                                  className={iconButton}
                                  title={t('regenerate')}
                                >
                                  <RefreshCw
                                    className="size-3.5"
                                    aria-hidden="true"
                                  />
                                  <span className="sr-only sm:not-sr-only">
                                    {t('regenerate')}
                                  </span>
                                </button>
                              )}
                            {msg.stopped && (
                              <span className="text-fl-hint px-1.5 font-mono text-amber-500">
                                {t('stopped')}
                              </span>
                            )}
                            {msg.created_at && (
                              <span className="text-fl-hint text-fl-muted-4 px-1.5 font-mono opacity-0 transition-opacity group-hover:opacity-100">
                                {formatTime(msg.created_at)}
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  )
                })
              )}
              {error && (
                <div className="text-fl-label text-fl-error-fg border-fl-error/30 flex flex-wrap items-center gap-3 border px-4 py-2 font-mono">
                  <span>
                    ✕{' '}
                    {error === 'No active study plan found'
                      ? tCommon('noActivePlan')
                      : error}
                  </span>
                  {!sending &&
                    activeId !== null &&
                    messages.length > 0 &&
                    messages[messages.length - 1].role === 'user' &&
                    messages[messages.length - 1].id !== undefined && (
                      <button
                        onClick={() => {
                          setMessages((prev) => [
                            ...prev,
                            { role: 'assistant', content: '' },
                          ])
                          void streamChat({ kind: 'regenerate' })
                        }}
                        className="text-fl-fg underline"
                      >
                        {tCommon('retry')}
                      </button>
                    )}
                </div>
              )}
              <div ref={bottomRef} />
            </div>

            {!atBottom && messages.length > 0 && (
              <button
                onClick={() => {
                  setAtBottom(true)
                  scrollBottom()
                }}
                className="border-fl-border bg-fl-surface text-fl-fg hover:bg-fl-surface-2 absolute bottom-4 left-1/2 inline-flex -translate-x-1/2 items-center gap-1.5 border px-3 py-1.5 font-mono text-xs shadow-sm"
              >
                <ArrowDown className="size-3.5" aria-hidden="true" />
                {t('scrollToBottom')}
              </button>
            )}
          </div>

          {/* Input */}
          <div className="border-fl-border bg-fl-bg shrink-0 border-t px-4 py-3">
            {freemiumExhausted ? (
              <PaywallBanner feature="chat" compact />
            ) : (
              <>
                <div className="border-fl-border bg-fl-surface focus-within:border-fl-border-2 flex items-end gap-2 border p-2 transition-colors">
                  <textarea
                    ref={inputRef}
                    rows={1}
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={handleInputKey}
                    disabled={loadingMsgs || voiceBusy}
                    maxLength={5000}
                    placeholder={t('placeholder')}
                    className="text-fl-fg placeholder:text-fl-border-2 max-h-[200px] min-h-[2.5rem] flex-1 resize-none bg-transparent px-2 py-2 font-sans text-base focus:outline-none disabled:opacity-40"
                  />
                  {options?.study_plan_id ? (
                    <VoiceRecorder
                      studyPlanId={options.study_plan_id}
                      maxSeconds={30}
                      disabled={sending || loadingMsgs}
                      onBusyChange={setVoiceBusy}
                      onTranscription={(text) => {
                        const spoken = text.trim()
                        if (!spoken) return
                        setInput((current) =>
                          current.trim() ? `${current.trimEnd()} ${spoken}` : spoken
                        )
                        requestAnimationFrame(() => inputRef.current?.focus())
                      }}
                      className="shrink-0"
                    />
                  ) : null}
                  {sending ? (
                    <button
                      onClick={stopReply}
                      className="border-fl-border text-fl-fg hover:bg-fl-bg inline-flex shrink-0 items-center gap-1.5 border px-4 py-2 font-mono text-sm font-bold tracking-widest uppercase transition-colors"
                    >
                      <Square
                        className="size-3.5 fill-current"
                        aria-hidden="true"
                      />
                      {t('stop')}
                    </button>
                  ) : (
                    <button
                      onClick={sendMessage}
                      disabled={!input.trim() || loadingMsgs || voiceBusy}
                      className="bg-fl-accent text-fl-accent-fg hover:bg-fl-accent/90 shrink-0 px-5 py-2 font-mono text-sm font-bold tracking-widest uppercase transition-colors disabled:opacity-30"
                    >
                      {t('send')}
                    </button>
                  )}
                </div>
                <p className="text-fl-hint text-fl-border-2 mt-2 font-mono tracking-wide">
                  {t('enterToSend')} · {t('shiftEnterNewline')}
                </p>
              </>
            )}
          </div>
        </div>

        <ConfirmDialog
          open={deletePending !== null}
          title={t('deleteTitle')}
          message={t('deleteMessage')}
          confirmLabel={t('deleteConfirm')}
          danger
          onConfirm={() =>
            deletePending !== null && deleteConversation(deletePending)
          }
          onCancel={() => setDeletePending(null)}
        />

        {/* Word-save tooltip */}
        {selectedWord && (
          <WordTooltip
            word={selectedWord}
            pos={tooltipPos}
            saveState={saveState}
            onSave={() => handleSaveWord()}
            onDismiss={dismissTooltip}
            labels={{
              saveWord: tCommon('saveWord'),
              wordSaved: tCommon('wordSaved'),
              wordAlreadySaved: tCommon('wordAlreadySaved'),
              wordSaveError: tCommon('wordSaveError'),
            }}
          />
        )}
      </div>
    </MaintenanceGate>
  )
}
