'use client'

import { useEffect, useState } from 'react'
import { useTranslations } from 'next-intl'
import {
  AudioLines,
  Bot,
  Loader2,
  Pencil,
  Plug,
  Plus,
  Power,
  Trash2,
} from 'lucide-react'

import { AdminNav } from '@/components/admin/AdminNav'
import {
  AdminBadge,
  AdminPageHeader,
  AdminPanel,
} from '@/components/admin/AdminShell'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { apiFetch } from '@/lib/api'
import { useConfigStore } from '@/store/config'

type Provider = 'openai_compatible' | 'openai' | 'anthropic'

interface AIChannel {
  id: number
  name: string
  provider: Provider
  base_url: string | null
  model: string | null
  reasoning_effort: string | null
  max_tokens: number | null
  priority: number
  is_enabled: boolean
  has_api_key: boolean
  api_key_hint: string | null
  api_key_unreadable: boolean
}

interface SpeechSettings {
  tts_channel_id: number | null
  tts_model: string | null
  tts_voice: string | null
  tts_speed: number
  tts_openai_voices: boolean
  stt_channel_id: number | null
  stt_model: string | null
}

interface RuntimeStatus {
  text_source: 'channels' | 'environment'
  text_channels: string[]
  environment_provider: string
  environment_model: string
  tts_source: 'channel' | 'environment'
  tts_channel: string | null
  stt_source: 'channel' | 'environment'
  stt_channel: string | null
}

interface AIConfig {
  channels: AIChannel[]
  speech: SpeechSettings
  runtime: RuntimeStatus
}

interface ChannelForm {
  id: number | null
  name: string
  provider: Provider
  base_url: string
  api_key: string
  clear_api_key: boolean
  model: string
  reasoning_effort: string
  max_tokens: string
  priority: string
  is_enabled: boolean
}

const PROVIDERS: Provider[] = ['openai_compatible', 'openai', 'anthropic']
const SPEECH_PROVIDERS: Provider[] = ['openai_compatible', 'openai']
const REASONING = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']

// Presets only fill in the form; every field stays editable.
const PRESETS: { key: string; name: string; provider: Provider; url: string }[] = [
  { key: 'openai', name: 'OpenAI', provider: 'openai', url: '' },
  { key: 'anthropic', name: 'Anthropic', provider: 'anthropic', url: '' },
  {
    key: 'deepseek',
    name: 'DeepSeek',
    provider: 'openai_compatible',
    url: 'https://api.deepseek.com/v1',
  },
  {
    key: 'openrouter',
    name: 'OpenRouter',
    provider: 'openai_compatible',
    url: 'https://openrouter.ai/api/v1',
  },
  {
    key: 'siliconflow',
    name: 'SiliconFlow',
    provider: 'openai_compatible',
    url: 'https://api.siliconflow.cn/v1',
  },
  {
    key: 'ollama',
    name: 'Ollama',
    provider: 'openai_compatible',
    url: 'http://host.docker.internal:11434/v1',
  },
]

const EMPTY_FORM: ChannelForm = {
  id: null,
  name: '',
  provider: 'openai_compatible',
  base_url: '',
  api_key: '',
  clear_api_key: false,
  model: '',
  reasoning_effort: '',
  max_tokens: '',
  priority: '0',
  is_enabled: true,
}

const inputClass =
  'border-fl-border bg-fl-bg text-fl-fg w-full border px-3 py-2 font-mono text-sm disabled:opacity-50'
const labelClass = 'text-fl-muted-2 block tracking-widest uppercase'
const secondaryButton =
  'border-fl-border text-fl-fg hover:bg-fl-bg inline-flex items-center justify-center gap-2 border px-3 py-2 font-mono text-xs tracking-widest uppercase transition-colors disabled:opacity-40'
const primaryButton =
  'bg-fl-accent text-fl-accent-fg hover:bg-fl-accent/90 inline-flex items-center justify-center gap-2 px-4 py-2 font-mono text-xs font-bold tracking-widest uppercase transition-colors disabled:opacity-40'

function formFromChannel(channel: AIChannel): ChannelForm {
  return {
    id: channel.id,
    name: channel.name,
    provider: channel.provider,
    base_url: channel.base_url ?? '',
    api_key: '',
    clear_api_key: false,
    model: channel.model ?? '',
    reasoning_effort: channel.reasoning_effort ?? '',
    max_tokens: channel.max_tokens ? String(channel.max_tokens) : '',
    priority: String(channel.priority),
    is_enabled: channel.is_enabled,
  }
}

function channelPayload(form: ChannelForm) {
  return {
    name: form.name,
    provider: form.provider,
    base_url: form.base_url.trim() || null,
    api_key: form.api_key.trim() || null,
    model: form.model.trim() || null,
    reasoning_effort:
      form.provider === 'openai' && form.reasoning_effort
        ? form.reasoning_effort
        : null,
    max_tokens:
      form.provider === 'anthropic' && form.max_tokens
        ? Number(form.max_tokens)
        : null,
    priority: Number(form.priority) || 0,
    is_enabled: form.is_enabled,
    ...(form.id !== null ? { clear_api_key: form.clear_api_key } : {}),
  }
}

async function errorDetail(response: Response): Promise<string | null> {
  try {
    const data = await response.json()
    if (typeof data.detail === 'string') return data.detail
    if (Array.isArray(data.detail) && data.detail[0]?.msg) {
      return String(data.detail[0].msg).replace(/^Value error, /, '')
    }
  } catch {
    // Fall back to the generic message.
  }
  return null
}

export default function AdminAIPage() {
  const t = useTranslations('admin')
  const [config, setConfig] = useState<AIConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [form, setForm] = useState<ChannelForm | null>(null)
  const [formError, setFormError] = useState('')
  const [saving, setSaving] = useState(false)
  const [probe, setProbe] = useState<{ ok: boolean; text: string } | null>(
    null
  )
  const [probing, setProbing] = useState<'test' | 'models' | null>(null)
  const [models, setModels] = useState<string[]>([])
  const [rowTesting, setRowTesting] = useState<number | null>(null)
  const [rowResult, setRowResult] = useState<
    Record<number, { ok: boolean; text: string }>
  >({})
  const [deletePending, setDeletePending] = useState<AIChannel | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [speech, setSpeech] = useState<SpeechSettings | null>(null)
  const [speechSaving, setSpeechSaving] = useState(false)
  const [speechNotice, setSpeechNotice] = useState('')
  const [speechError, setSpeechError] = useState('')

  function applyConfig(data: AIConfig) {
    setConfig(data)
    setSpeech(data.speech)
  }

  useEffect(() => {
    async function load() {
      try {
        const response = await apiFetch('/api/admin/ai')
        if (!response.ok) throw new Error('load failed')
        applyConfig(await response.json())
      } catch {
        setError(t('aiChannels.loadError'))
      } finally {
        setLoading(false)
      }
    }
    load()
    // Loaded once; later changes come back from each mutation response.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function openForm(next: ChannelForm) {
    setForm(next)
    setFormError('')
    setProbe(null)
    setModels([])
    setNotice('')
  }

  function updateForm(patch: Partial<ChannelForm>) {
    setForm((current) => (current ? { ...current, ...patch } : current))
  }

  function applyPreset(key: string) {
    const preset = PRESETS.find((item) => item.key === key)
    if (!preset || !form) return
    updateForm({
      name: form.name || preset.name,
      provider: preset.provider,
      base_url: preset.url,
    })
  }

  async function saveChannel() {
    if (!form) return
    setSaving(true)
    setFormError('')
    try {
      const response = await apiFetch(
        form.id === null
          ? '/api/admin/ai/channels'
          : `/api/admin/ai/channels/${form.id}`,
        {
          method: form.id === null ? 'POST' : 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(channelPayload(form)),
        }
      )
      if (!response.ok) {
        setFormError(
          (await errorDetail(response)) ?? t('aiChannels.saveError')
        )
        return
      }
      applyConfig(await response.json())
      setForm(null)
      setNotice(t('aiChannels.saved'))
    } catch {
      setFormError(t('aiChannels.saveError'))
    } finally {
      setSaving(false)
    }
  }

  async function runProbe(
    kind: 'test' | 'models',
    body: Record<string, unknown>
  ): Promise<{ ok: boolean; text: string; models?: string[] }> {
    try {
      const response = await apiFetch(`/api/admin/ai/${kind}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!response.ok) {
        const detail =
          (await errorDetail(response)) ?? t('aiChannels.saveError')
        return {
          ok: false,
          text: t('aiChannels.testFailed', { error: detail }),
        }
      }
      const data = await response.json()
      if (!data.ok) {
        return {
          ok: false,
          text: t('aiChannels.testFailed', { error: data.error ?? '' }),
        }
      }
      if (kind === 'models') {
        return {
          ok: true,
          text: t('aiChannels.modelsLoaded', { count: data.models.length }),
          models: data.models,
        }
      }
      return {
        ok: true,
        text: t('aiChannels.testOk', {
          ms: data.latency_ms,
          reply: data.reply ?? '',
        }),
      }
    } catch {
      return {
        ok: false,
        text: t('aiChannels.testFailed', { error: '' }),
      }
    }
  }

  async function probeForm(kind: 'test' | 'models') {
    if (!form) return
    setProbing(kind)
    setProbe(null)
    const result = await runProbe(kind, {
      channel_id: form.id,
      provider: form.provider,
      base_url: form.base_url.trim() || null,
      api_key: form.api_key.trim() || null,
      model: form.model.trim() || null,
    })
    if (result.models) setModels(result.models)
    setProbe({ ok: result.ok, text: result.text })
    setProbing(null)
  }

  async function testRow(channel: AIChannel) {
    setRowTesting(channel.id)
    const result = await runProbe('test', {
      channel_id: channel.id,
      provider: channel.provider,
      base_url: channel.base_url,
      model: channel.model,
    })
    setRowResult((current) => ({ ...current, [channel.id]: result }))
    setRowTesting(null)
  }

  async function toggleChannel(channel: AIChannel) {
    setError('')
    const next = { ...formFromChannel(channel), is_enabled: !channel.is_enabled }
    try {
      const response = await apiFetch(`/api/admin/ai/channels/${channel.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(channelPayload(next)),
      })
      if (!response.ok) {
        setError((await errorDetail(response)) ?? t('aiChannels.saveError'))
        return
      }
      applyConfig(await response.json())
    } catch {
      setError(t('aiChannels.saveError'))
    }
  }

  async function deleteChannel() {
    if (!deletePending) return
    setDeleting(true)
    try {
      const response = await apiFetch(
        `/api/admin/ai/channels/${deletePending.id}`,
        { method: 'DELETE' }
      )
      if (!response.ok) throw new Error('delete failed')
      applyConfig(await response.json())
      if (form?.id === deletePending.id) setForm(null)
      setDeletePending(null)
    } catch {
      setError(t('aiChannels.saveError'))
      setDeletePending(null)
    } finally {
      setDeleting(false)
    }
  }

  async function saveSpeech() {
    if (!speech) return
    setSpeechSaving(true)
    setSpeechNotice('')
    setSpeechError('')
    try {
      const response = await apiFetch('/api/admin/ai/speech', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(speech),
      })
      if (!response.ok) {
        setSpeechError(
          (await errorDetail(response)) ?? t('aiChannels.saveError')
        )
        return
      }
      applyConfig(await response.json())
      setSpeechNotice(t('aiChannels.saved'))
      // Learner voice options depend on the active speech service.
      useConfigStore.setState({ loaded: false })
      await useConfigStore.getState().load()
    } catch {
      setSpeechError(t('aiChannels.saveError'))
    } finally {
      setSpeechSaving(false)
    }
  }

  function providerLabel(provider: Provider) {
    return t(
      `aiChannels.provider${
        provider === 'openai_compatible'
          ? 'OpenaiCompatible'
          : provider === 'openai'
            ? 'Openai'
            : 'Anthropic'
      }`
    )
  }

  const runtime = config?.runtime
  const speechChannels = (config?.channels ?? []).filter((channel) =>
    SPEECH_PROVIDERS.includes(channel.provider)
  )
  const editing = form?.id != null ? config?.channels.find((c) => c.id === form.id) : null

  return (
    <div className="mx-auto max-w-6xl space-y-4 p-6">
      <AdminPageHeader
        eyebrow={`${t('title')} / ${t('ai')}`}
        title={t('aiChannels.title')}
        actions={
          !loading &&
          !error && (
            <button
              type="button"
              onClick={() => openForm({ ...EMPTY_FORM })}
              className={primaryButton}
            >
              <Plus className="size-3.5" aria-hidden="true" />
              {t('aiChannels.addChannel')}
            </button>
          )
        }
      />

      <AdminNav />

      <p className="text-fl-muted-2 font-mono text-xs">
        {t('aiChannels.description')}
      </p>

      {error && (
        <p
          role="alert"
          className="border-fl-error/40 text-fl-error border px-4 py-3 font-mono text-xs"
        >
          {error}
        </p>
      )}
      {notice && (
        <p className="border-fl-accent/40 text-fl-accent border px-4 py-3 font-mono text-xs">
          {notice}
        </p>
      )}

      {loading ? (
        <div className="text-fl-muted-2 flex items-center gap-2 font-mono text-xs">
          <Loader2 className="size-4 animate-spin" aria-hidden="true" />
          {t('loading')}
        </div>
      ) : (
        config &&
        runtime && (
          <>
            <AdminPanel title={t('aiChannels.runtimeTitle')}>
              <dl className="divide-fl-border grid divide-y font-mono text-xs md:grid-cols-3 md:divide-x md:divide-y-0">
                <div className="px-5 py-4">
                  <dt className={labelClass}>{t('aiChannels.textAi')}</dt>
                  <dd className="text-fl-fg mt-2 break-words">
                    {runtime.text_source === 'channels'
                      ? t('aiChannels.sourceChannels', {
                          names: runtime.text_channels.join(' → '),
                        })
                      : t('aiChannels.sourceEnvironment', {
                          provider: runtime.environment_provider,
                          model: runtime.environment_model || '—',
                        })}
                  </dd>
                </div>
                <div className="px-5 py-4">
                  <dt className={labelClass}>{t('aiChannels.tts')}</dt>
                  <dd className="text-fl-fg mt-2 break-words">
                    {runtime.tts_channel
                      ? t('aiChannels.sourceChannel', {
                          name: runtime.tts_channel,
                        })
                      : t('aiChannels.speechEnvironment')}
                  </dd>
                </div>
                <div className="px-5 py-4">
                  <dt className={labelClass}>{t('aiChannels.stt')}</dt>
                  <dd className="text-fl-fg mt-2 break-words">
                    {runtime.stt_channel
                      ? t('aiChannels.sourceChannel', {
                          name: runtime.stt_channel,
                        })
                      : t('aiChannels.speechEnvironment')}
                  </dd>
                </div>
              </dl>
            </AdminPanel>

            {form && (
              <section className="border-fl-accent/40 bg-fl-surface border p-5">
                <h2 className="text-fl-fg mb-4 font-mono text-sm font-bold tracking-widest uppercase">
                  {form.id === null
                    ? t('aiChannels.newChannel')
                    : t('aiChannels.editChannel')}
                </h2>
                {formError && (
                  <p
                    role="alert"
                    className="border-fl-error/40 text-fl-error mb-4 border px-4 py-3 font-mono text-xs"
                  >
                    {formError}
                  </p>
                )}
                <div className="grid gap-4 md:grid-cols-2">
                  {form.id === null && (
                    <label className="space-y-2 font-mono text-xs md:col-span-2">
                      <span className={labelClass}>
                        {t('aiChannels.preset')}
                      </span>
                      <select
                        defaultValue=""
                        onChange={(event) => applyPreset(event.target.value)}
                        className={inputClass}
                      >
                        <option value="">{t('aiChannels.presetCustom')}</option>
                        {PRESETS.map((preset) => (
                          <option key={preset.key} value={preset.key}>
                            {preset.name}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  <label className="space-y-2 font-mono text-xs">
                    <span className={labelClass}>
                      {t('aiChannels.fieldName')}
                    </span>
                    <input
                      value={form.name}
                      onChange={(event) =>
                        updateForm({ name: event.target.value })
                      }
                      maxLength={80}
                      className={inputClass}
                    />
                  </label>
                  <label className="space-y-2 font-mono text-xs">
                    <span className={labelClass}>
                      {t('aiChannels.fieldProvider')}
                    </span>
                    <select
                      value={form.provider}
                      onChange={(event) =>
                        updateForm({ provider: event.target.value as Provider })
                      }
                      className={inputClass}
                    >
                      {PROVIDERS.map((provider) => (
                        <option key={provider} value={provider}>
                          {providerLabel(provider)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="space-y-2 font-mono text-xs md:col-span-2">
                    <span className={labelClass}>
                      {t('aiChannels.fieldBaseUrl')}
                    </span>
                    <input
                      value={form.base_url}
                      onChange={(event) =>
                        updateForm({ base_url: event.target.value })
                      }
                      placeholder="https://api.example.com/v1"
                      maxLength={500}
                      className={inputClass}
                    />
                    <span className="text-fl-muted-3 block">
                      {t('aiChannels.baseUrlHint')}
                    </span>
                  </label>
                  <label className="space-y-2 font-mono text-xs md:col-span-2">
                    <span className={labelClass}>
                      {t('aiChannels.fieldApiKey')}
                    </span>
                    <input
                      type="password"
                      autoComplete="off"
                      value={form.api_key}
                      onChange={(event) =>
                        updateForm({ api_key: event.target.value })
                      }
                      disabled={form.clear_api_key}
                      placeholder={
                        editing?.has_api_key && !editing.api_key_unreadable
                          ? t('aiChannels.apiKeyKeep', {
                              hint: editing.api_key_hint ?? '',
                            })
                          : 'sk-…'
                      }
                      className={inputClass}
                    />
                    {editing?.api_key_unreadable && (
                      <span className="text-fl-error block">
                        {t('aiChannels.apiKeyUnreadable')}
                      </span>
                    )}
                    {editing?.has_api_key && (
                      <span className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={form.clear_api_key}
                          onChange={(event) =>
                            updateForm({
                              clear_api_key: event.target.checked,
                              api_key: '',
                            })
                          }
                          className="accent-fl-accent size-4"
                        />
                        <span className="text-fl-muted-2">
                          {t('aiChannels.clearApiKey')}
                        </span>
                      </span>
                    )}
                  </label>
                  <label className="space-y-2 font-mono text-xs md:col-span-2">
                    <span className={labelClass}>
                      {t('aiChannels.fieldModel')}
                    </span>
                    <div className="flex gap-2">
                      <input
                        value={form.model}
                        onChange={(event) =>
                          updateForm({ model: event.target.value })
                        }
                        list="ai-channel-models"
                        maxLength={200}
                        className={inputClass}
                      />
                      <button
                        type="button"
                        onClick={() => probeForm('models')}
                        disabled={probing !== null}
                        className={`${secondaryButton} shrink-0`}
                      >
                        {probing === 'models' && (
                          <Loader2
                            className="size-3.5 animate-spin"
                            aria-hidden="true"
                          />
                        )}
                        {t('aiChannels.fetchModels')}
                      </button>
                    </div>
                    <datalist id="ai-channel-models">
                      {models.map((model) => (
                        <option key={model} value={model} />
                      ))}
                    </datalist>
                    <span className="text-fl-muted-3 block">
                      {t('aiChannels.modelHint')}
                    </span>
                  </label>
                  {form.provider === 'openai' && (
                    <label className="space-y-2 font-mono text-xs">
                      <span className={labelClass}>
                        {t('aiChannels.fieldReasoning')}
                      </span>
                      <select
                        value={form.reasoning_effort}
                        onChange={(event) =>
                          updateForm({ reasoning_effort: event.target.value })
                        }
                        className={inputClass}
                      >
                        <option value="">
                          {t('aiChannels.reasoningDefault')}
                        </option>
                        {REASONING.map((effort) => (
                          <option key={effort} value={effort}>
                            {effort}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {form.provider === 'anthropic' && (
                    <label className="space-y-2 font-mono text-xs">
                      <span className={labelClass}>
                        {t('aiChannels.fieldMaxTokens')}
                      </span>
                      <input
                        type="number"
                        min={1}
                        max={200000}
                        value={form.max_tokens}
                        onChange={(event) =>
                          updateForm({ max_tokens: event.target.value })
                        }
                        placeholder="8192"
                        className={inputClass}
                      />
                    </label>
                  )}
                  <label className="space-y-2 font-mono text-xs">
                    <span className={labelClass}>
                      {t('aiChannels.fieldPriority')}
                    </span>
                    <input
                      type="number"
                      min={-1000}
                      max={1000}
                      value={form.priority}
                      onChange={(event) =>
                        updateForm({ priority: event.target.value })
                      }
                      className={inputClass}
                    />
                    <span className="text-fl-muted-3 block">
                      {t('aiChannels.priorityHint')}
                    </span>
                  </label>
                  <label className="flex items-center gap-3 font-mono text-xs">
                    <input
                      type="checkbox"
                      checked={form.is_enabled}
                      onChange={(event) =>
                        updateForm({ is_enabled: event.target.checked })
                      }
                      className="accent-fl-accent size-4"
                    />
                    <span className="text-fl-fg font-bold">
                      {t('aiChannels.fieldEnabled')}
                    </span>
                  </label>
                </div>

                {probe && (
                  <p
                    className={`mt-4 border px-4 py-3 font-mono text-xs break-words ${
                      probe.ok
                        ? 'border-fl-accent/40 text-fl-accent'
                        : 'border-fl-error/40 text-fl-error'
                    }`}
                  >
                    {probe.text}
                  </p>
                )}

                <div className="mt-5 flex flex-wrap justify-end gap-2">
                  <button
                    type="button"
                    onClick={() => probeForm('test')}
                    disabled={probing !== null || !form.model.trim()}
                    className={secondaryButton}
                  >
                    {probing === 'test' ? (
                      <Loader2
                        className="size-3.5 animate-spin"
                        aria-hidden="true"
                      />
                    ) : (
                      <Plug className="size-3.5" aria-hidden="true" />
                    )}
                    {t('aiChannels.test')}
                  </button>
                  <button
                    type="button"
                    onClick={() => setForm(null)}
                    className={secondaryButton}
                  >
                    {t('aiChannels.cancel')}
                  </button>
                  <button
                    type="button"
                    onClick={saveChannel}
                    disabled={saving || !form.name.trim()}
                    className={primaryButton}
                  >
                    {saving && (
                      <Loader2
                        className="size-3.5 animate-spin"
                        aria-hidden="true"
                      />
                    )}
                    {t('aiChannels.save')}
                  </button>
                </div>
              </section>
            )}

            <AdminPanel
              title={t('aiChannels.channelsTitle')}
              meta={
                <span className="text-fl-muted-3 font-mono text-xs">
                  {config.channels.length}
                </span>
              }
            >
              {config.channels.length === 0 ? (
                <p className="text-fl-muted-2 px-5 py-6 font-mono text-xs">
                  {t('aiChannels.noChannels')}
                </p>
              ) : (
                <ul className="divide-fl-border divide-y">
                  {config.channels.map((channel) => {
                    const result = rowResult[channel.id]
                    return (
                      <li key={channel.id} className="px-5 py-4">
                        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
                          <div className="min-w-0 space-y-1.5">
                            <div className="flex flex-wrap items-center gap-2">
                              <Bot
                                className="text-fl-muted-3 size-4"
                                aria-hidden="true"
                              />
                              <span className="text-fl-fg font-mono text-sm font-bold">
                                {channel.name}
                              </span>
                              <AdminBadge
                                tone={channel.is_enabled ? 'success' : 'neutral'}
                              >
                                {channel.is_enabled
                                  ? t('aiChannels.enabled')
                                  : t('aiChannels.disabled')}
                              </AdminBadge>
                              <AdminBadge tone="info">
                                {providerLabel(channel.provider)}
                              </AdminBadge>
                              {channel.api_key_unreadable ? (
                                <AdminBadge tone="danger">
                                  {t('aiChannels.keyUnreadable')}
                                </AdminBadge>
                              ) : (
                                !channel.has_api_key && (
                                  <AdminBadge tone="warning">
                                    {t('aiChannels.noKey')}
                                  </AdminBadge>
                                )
                              )}
                            </div>
                            <p className="text-fl-muted-2 font-mono text-xs break-all">
                              {channel.model ?? t('aiChannels.speechOnly')}
                              {' · '}
                              {t('aiChannels.priorityValue', {
                                priority: channel.priority,
                              })}
                              {channel.base_url && ` · ${channel.base_url}`}
                              {channel.api_key_hint &&
                                ` · ${channel.api_key_hint}`}
                            </p>
                          </div>
                          <div className="flex shrink-0 flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={() => testRow(channel)}
                              disabled={
                                rowTesting !== null || !channel.model
                              }
                              className={secondaryButton}
                            >
                              {rowTesting === channel.id ? (
                                <Loader2
                                  className="size-3.5 animate-spin"
                                  aria-hidden="true"
                                />
                              ) : (
                                <Plug className="size-3.5" aria-hidden="true" />
                              )}
                              {t('aiChannels.test')}
                            </button>
                            <button
                              type="button"
                              onClick={() => openForm(formFromChannel(channel))}
                              className={secondaryButton}
                            >
                              <Pencil className="size-3.5" aria-hidden="true" />
                              {t('aiChannels.edit')}
                            </button>
                            <button
                              type="button"
                              onClick={() => toggleChannel(channel)}
                              className={secondaryButton}
                            >
                              <Power className="size-3.5" aria-hidden="true" />
                              {channel.is_enabled
                                ? t('aiChannels.disable')
                                : t('aiChannels.enable')}
                            </button>
                            <button
                              type="button"
                              onClick={() => setDeletePending(channel)}
                              className={`${secondaryButton} text-fl-error`}
                            >
                              <Trash2 className="size-3.5" aria-hidden="true" />
                              {t('aiChannels.delete')}
                            </button>
                          </div>
                        </div>
                        {result && (
                          <p
                            className={`mt-3 font-mono text-xs break-words ${
                              result.ok ? 'text-fl-accent' : 'text-fl-error'
                            }`}
                          >
                            {result.text}
                          </p>
                        )}
                      </li>
                    )
                  })}
                </ul>
              )}
            </AdminPanel>

            {speech && (
              <section className="border-fl-border bg-fl-surface border p-5">
                <div className="border-fl-border mb-5 flex gap-3 border-b pb-4">
                  <AudioLines
                    className="text-fl-accent mt-0.5 size-5 shrink-0"
                    aria-hidden="true"
                  />
                  <div>
                    <h2 className="text-fl-fg font-mono text-sm font-bold tracking-widest uppercase">
                      {t('aiChannels.speechTitle')}
                    </h2>
                    <p className="text-fl-muted-2 mt-1 font-mono text-xs">
                      {t('aiChannels.speechDescription')}
                    </p>
                  </div>
                </div>
                {speechError && (
                  <p
                    role="alert"
                    className="border-fl-error/40 text-fl-error mb-4 border px-4 py-3 font-mono text-xs"
                  >
                    {speechError}
                  </p>
                )}
                {speechNotice && (
                  <p className="border-fl-accent/40 text-fl-accent mb-4 border px-4 py-3 font-mono text-xs">
                    {speechNotice}
                  </p>
                )}
                <div className="grid gap-6 md:grid-cols-2">
                  <div className="space-y-4">
                    <label className="space-y-2 font-mono text-xs">
                      <span className={labelClass}>
                        {t('aiChannels.ttsChannel')}
                      </span>
                      <select
                        value={speech.tts_channel_id ?? ''}
                        onChange={(event) =>
                          setSpeech({
                            ...speech,
                            tts_channel_id: event.target.value
                              ? Number(event.target.value)
                              : null,
                          })
                        }
                        className={inputClass}
                      >
                        <option value="">
                          {t('aiChannels.speechEnvironment')}
                        </option>
                        {speechChannels.map((channel) => (
                          <option key={channel.id} value={channel.id}>
                            {channel.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="space-y-2 font-mono text-xs">
                      <span className={labelClass}>
                        {t('aiChannels.ttsModel')}
                      </span>
                      <input
                        value={speech.tts_model ?? ''}
                        onChange={(event) =>
                          setSpeech({ ...speech, tts_model: event.target.value })
                        }
                        placeholder="tts-1"
                        disabled={speech.tts_channel_id === null}
                        className={inputClass}
                      />
                    </label>
                    <label className="space-y-2 font-mono text-xs">
                      <span className={labelClass}>
                        {t('aiChannels.ttsVoice')}
                      </span>
                      <input
                        value={speech.tts_voice ?? ''}
                        onChange={(event) =>
                          setSpeech({ ...speech, tts_voice: event.target.value })
                        }
                        placeholder="nova"
                        disabled={speech.tts_channel_id === null}
                        className={inputClass}
                      />
                    </label>
                    <label className="space-y-2 font-mono text-xs">
                      <span className={labelClass}>
                        {t('aiChannels.ttsSpeed')}
                      </span>
                      <input
                        type="number"
                        min={0.25}
                        max={4}
                        step={0.05}
                        value={speech.tts_speed}
                        onChange={(event) =>
                          setSpeech({
                            ...speech,
                            tts_speed: Number(event.target.value) || 1,
                          })
                        }
                        disabled={speech.tts_channel_id === null}
                        className={inputClass}
                      />
                    </label>
                    <label className="flex gap-3 font-mono text-xs">
                      <input
                        type="checkbox"
                        checked={speech.tts_openai_voices}
                        onChange={(event) =>
                          setSpeech({
                            ...speech,
                            tts_openai_voices: event.target.checked,
                          })
                        }
                        disabled={speech.tts_channel_id === null}
                        className="accent-fl-accent mt-0.5 size-4 shrink-0"
                      />
                      <span>
                        <span className="text-fl-fg block font-bold">
                          {t('aiChannels.ttsOpenaiVoices')}
                        </span>
                        <span className="text-fl-muted-3 mt-1 block">
                          {t('aiChannels.ttsOpenaiVoicesHint')}
                        </span>
                      </span>
                    </label>
                  </div>
                  <div className="space-y-4">
                    <label className="space-y-2 font-mono text-xs">
                      <span className={labelClass}>
                        {t('aiChannels.sttChannel')}
                      </span>
                      <select
                        value={speech.stt_channel_id ?? ''}
                        onChange={(event) =>
                          setSpeech({
                            ...speech,
                            stt_channel_id: event.target.value
                              ? Number(event.target.value)
                              : null,
                          })
                        }
                        className={inputClass}
                      >
                        <option value="">
                          {t('aiChannels.speechEnvironment')}
                        </option>
                        {speechChannels.map((channel) => (
                          <option key={channel.id} value={channel.id}>
                            {channel.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="space-y-2 font-mono text-xs">
                      <span className={labelClass}>
                        {t('aiChannels.sttModel')}
                      </span>
                      <input
                        value={speech.stt_model ?? ''}
                        onChange={(event) =>
                          setSpeech({ ...speech, stt_model: event.target.value })
                        }
                        placeholder="whisper-1"
                        disabled={speech.stt_channel_id === null}
                        className={inputClass}
                      />
                    </label>
                    <p className="text-fl-muted-3 font-mono text-xs">
                      {t('aiChannels.speechHint')}
                    </p>
                  </div>
                </div>
                <div className="mt-5 flex justify-end">
                  <button
                    type="button"
                    onClick={saveSpeech}
                    disabled={speechSaving}
                    className={primaryButton}
                  >
                    {speechSaving && (
                      <Loader2
                        className="size-3.5 animate-spin"
                        aria-hidden="true"
                      />
                    )}
                    {t('aiChannels.saveSpeech')}
                  </button>
                </div>
              </section>
            )}
          </>
        )
      )}

      <ConfirmDialog
        open={deletePending !== null}
        title={t('aiChannels.deleteTitle')}
        message={t('aiChannels.deleteMessage')}
        confirmLabel={t('aiChannels.delete')}
        danger
        confirming={deleting}
        onConfirm={deleteChannel}
        onCancel={() => setDeletePending(null)}
      />
    </div>
  )
}
