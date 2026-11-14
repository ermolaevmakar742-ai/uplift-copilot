/* ARES Creative Intelligence UI. Uses the panel's authenticated fetch (window.__aresPanel). All remote text goes through textContent — never innerHTML.
   Operator-facing copy only: backend codes, env names, model ids and file paths are mapped to plain language here, never shown raw. */
(function () {
  'use strict';
  const P = () => window.__aresPanel;
  const L = () => (P() && P().getLang && P().getLang()) || 'en';
  const D = {
    en: {
      tab_radar: 'Trend radar', tab_analysis: 'Video teardown', tab_forge: 'Prompt studio', tab_lib: 'Library',
      search_src: 'Search platforms', platform: 'Platform', keywords: 'Keywords', niche: 'Niche', geo: 'Country', language: 'Language', within: 'Published within, days', maxdur: 'Max length, s', order: 'Order', source: 'Source', auto: 'Automatic',
      by_views: 'Most viewed', by_date: 'Newest', search: 'Search', add_url: 'Add a video by link', add: 'Add', import: 'Import a table', filters: 'Filter saved videos', apply: 'Apply', reset: 'Reset',
      minv: 'Views from', maxv: 'Views up to', minf: 'Followers from', disc: 'Found within, days', vtype: 'Video type', delivery: 'Delivery', tstatus: 'Trend status', sort: 'Sort by', s_anomaly: 'Anomaly index', s_views: 'Views', s_eng: 'Engagement', s_date: 'Publish date', s_disc: 'Date found',
      any: 'Any', none_found: 'No videos yet. Add one by link, import a table or search a connected platform. There is no sample data here.', views: 'views', likes: 'likes', comments: 'comments', shares: 'shares', followers: 'followers',
      open_src: 'Original', save: 'Save', saved: 'Saved', unsaved: 'Removed from library', analyze: 'Tear down', details: 'Open', no_score: 'no index', score_tip: 'Anomaly index: a sorting signal, not a probability of going viral',
      upload_title: 'Video', pick_ref: 'Link to a saved video', new_upload: '— new video —', drop: 'Drop a video here', drop_sub: 'or click to choose a file', limits: 'Up to {mb} MB · up to {min} min · {fmt}', choose_file: 'Choose a video file first',
      upload_go: 'Upload and tear down', jobs: 'Teardowns', retry: 'Retry', report: 'Teardown', facts: 'Measured facts', frames: 'Key frames', timeline: 'Timeline', to_forge: 'Write prompts from this',
      sec_a: 'Summary', sec_c: 'Actor and action', sec_d: 'Camera and staging', sec_e: 'Sound', sec_f: 'Script mechanics', sec_g: 'Why it may work', limits_h: 'Limitations',
      forge_pick: 'Video with a finished teardown', gen: 'Write prompt kit', regen: 'Write a new version', copy: 'Copy', copied: 'Copied', copy_all: 'Copy all', copy_main: 'Copy main + negative', generator: 'Video generator', goal: 'Goal', duration: 'Length, s', aspect: 'Format', audience: 'Audience',
      character: 'Character', voiceover: 'Voice-over', onscreen: 'On-screen text', shots: 'Separate shot prompts', brief: 'Extra instructions (optional)', custom_name: 'Tool name', custom_clip: 'Max clip, s', custom_chars: 'Max prompt length', custom_neg: 'Supports a negative prompt', custom_audio: 'Generates sound',
      p1: 'Creative blueprint', p2: 'Storyboard', p3: 'Main video prompt', p4: 'Shot prompts', p5: 'Character', p6: 'Environment', p7: 'Camera and motion', p8: 'Sound design', p9: 'Negative prompt', p10: 'Three adaptations', p11: 'Production checklist', prov: 'Taken from the source vs. proposed by AI',
      from_src: 'Taken from the source', by_ai: 'Proposed by AI', shot: 'Shot', continuity: 'Continuity', up_to_chars: 'up to {n} characters',
      lib_empty: 'The library is empty. Save a video from the radar or upload one for a teardown.', prod: 'Production', notes: 'Notes', tags: 'Tags, comma separated', saveb: 'Save changes', metrics_hist: 'Measurements', add_meas: 'Add measurement', refresh_src: 'Re-read from source', pubs: 'My publications', add_pub: 'Add publication', pub_link: 'Link to my post',
      exp_md: 'Export · Markdown', exp_json: 'Export · JSON', del: 'Delete', confirm_del: 'Delete this video with its teardowns and prompts?', anomaly: 'Anomaly index', missing: 'Not counted — no data', prompts: 'Prompt kits', analyses: 'Teardowns', weight: 'weight',
      disclaimer: 'A ranking signal built from the metrics above. It is not a probability of going viral.', when: 'When', src_col: 'Source',
      loading: 'Loading…', err: 'Error', done: 'Done', started: 'Started', refs_n: 'videos', found: 'Found {n}, new {m}', added_meta: 'Added. Popularity metrics are not available by link — you can enter them by hand.',
      st_ready: 'working', st_needs_key: 'access not set up', st_metadata_only: 'by link only', st_not_connected: 'not connected', st_error: 'request failed',
      cov_title: 'What is connected', cov_search: 'Trend search', cov_metrics: 'Popularity metrics', cov_meta: 'Data by link', cov_note: 'Nothing here is simulated. Platforms without a connection can still be filled by link, by table import or by hand. Connections are set up by the server administrator.',
      cap_teardown: 'Video teardown', cap_speech: 'Speech transcription', cap_on: 'working', cap_off: 'not connected',
      no_metrics_n: 'without popularity metrics — their anomaly index is unavailable.', search_blocked: 'Search for this platform is not connected yet', retry_later: 'Retry',
      credits: 'The AI provider has run out of credits. Nothing was generated — retry after the balance is topped up.',
      st_queued: 'queued', st_processing: 'in progress', st_completed: 'done', st_failed: 'failed', writing: 'The model is writing the prompt kit…', analysing: 'The video is being analysed…', nothing_gen: 'Nothing was generated.',
      reused_an: 'This file was already analysed — showing the saved teardown.', reused_pr: 'A kit with the same settings exists — showing it. Use “Write a new version” for another.', need_analysis: 'This video has no finished teardown yet — make one on the “Video teardown” tab.', pick_ref_first: 'Choose a video first',
      social_note: 'If a platform does not let ARES download the video, save your own copy and upload it here. A cover image alone is never treated as a teardown.',
      inj: 'The video text contained instruction-like phrases. They were treated as plain data and ignored as commands.',
      imp_note: 'CSV with a header row, or a JSON list. Empty cells stay empty — nothing is estimated.', imported: 'Imported: {n}', imp_errors: 'rows with errors: {n}',
      f_len: 'Length and format', f_audio: 'Sound', f_edit: 'Editing', f_text: 'Speech transcript', f_frames: 'Frames analysed', f_none: 'no sound', f_yes: 'present', f_pauses: 'pauses: {n}',
      f_cuts: '{n} cuts · average shot {s} s', f_chars: '{n} characters', f_tr_off: 'not available — transcription is not connected',
      b_observed: 'seen in video', b_inferred: 'assumption', b_unknown: 'no data',
      e_generic: 'Something went wrong. Please try again.', e_http_402: 'The AI provider has run out of credits. Nothing was generated — retry after the balance is topped up.',
      e_llm_not_configured: 'The AI model is not set up. Ask the administrator.', e_llm: 'The AI model did not answer correctly. Try again later.', e_media: 'The server could not process this video. Try again later.',
      e_file_too_large: 'The file is too large.', e_too_long: 'The video is too long.', e_bad_file: 'This file does not look like a supported video.', e_upload_missing: 'The original file was already removed — upload it again.',
      e_no_search: 'Search for this platform is not connected yet.', e_analysis_required: 'Make a teardown of this video first.', e_not_found: 'Not found.', e_no_url: 'Paste a link.', e_invalid: 'Check the entered values.',
      e_no_records: 'The file has no records.', e_not_failed: 'Only a failed run can be retried.', e_unavailable: 'The module is temporarily unavailable.' },
    ru: {
      tab_radar: 'Радар трендов', tab_analysis: 'Разбор видео', tab_forge: 'Генератор промптов', tab_lib: 'Библиотека',
      search_src: 'Поиск по платформам', platform: 'Платформа', keywords: 'Ключевые слова', niche: 'Ниша', geo: 'Страна', language: 'Язык', within: 'Опубликовано за, дней', maxdur: 'Длина до, с', order: 'Порядок', source: 'Источник', auto: 'Автоматически',
      by_views: 'По просмотрам', by_date: 'Сначала новые', search: 'Искать', add_url: 'Добавить видео по ссылке', add: 'Добавить', import: 'Импорт таблицы', filters: 'Фильтр сохранённых видео', apply: 'Применить', reset: 'Сбросить',
      minv: 'Просмотры от', maxv: 'Просмотры до', minf: 'Подписчики от', disc: 'Найдено за, дней', vtype: 'Тип видео', delivery: 'Подача', tstatus: 'Статус тренда', sort: 'Сортировка', s_anomaly: 'Индекс аномалии', s_views: 'Просмотры', s_eng: 'Вовлечённость', s_date: 'Дата публикации', s_disc: 'Дата находки',
      any: 'Любой', none_found: 'Видео пока нет. Добавьте ссылку, импортируйте таблицу или выполните поиск по подключённой платформе. Тестовых данных здесь нет.', views: 'просм.', likes: 'лайков', comments: 'комм.', shares: 'репостов', followers: 'подписчиков',
      open_src: 'Оригинал', save: 'Сохранить', saved: 'Сохранено', unsaved: 'Убрано из библиотеки', analyze: 'Разобрать', details: 'Открыть', no_score: 'нет индекса', score_tip: 'Индекс аномалии — сигнал для сортировки, а не вероятность вирусности',
      upload_title: 'Видео', pick_ref: 'Привязать к сохранённому видео', new_upload: '— новое видео —', drop: 'Перетащите видео сюда', drop_sub: 'или нажмите, чтобы выбрать файл', limits: 'До {mb} МБ · до {min} мин · {fmt}', choose_file: 'Сначала выберите видеофайл',
      upload_go: 'Загрузить и разобрать', jobs: 'Разборы', retry: 'Повторить', report: 'Разбор', facts: 'Измеренные факты', frames: 'Ключевые кадры', timeline: 'Хронология', to_forge: 'Написать промпты по разбору',
      sec_a: 'Коротко', sec_c: 'Герой и действие', sec_d: 'Камера и постановка', sec_e: 'Звук', sec_f: 'Механика сценария', sec_g: 'Почему может сработать', limits_h: 'Ограничения',
      forge_pick: 'Видео с готовым разбором', gen: 'Написать набор промптов', regen: 'Написать новую версию', copy: 'Копировать', copied: 'Скопировано', copy_all: 'Копировать всё', copy_main: 'Копировать основной + негативный', generator: 'Генератор видео', goal: 'Цель', duration: 'Длина, с', aspect: 'Формат', audience: 'Аудитория',
      character: 'Персонаж', voiceover: 'Закадровый голос', onscreen: 'Текст в кадре', shots: 'Отдельные промпты по сценам', brief: 'Дополнительные указания (необязательно)', custom_name: 'Название инструмента', custom_clip: 'Длина клипа до, с', custom_chars: 'Длина промпта до', custom_neg: 'Поддерживает негативный промпт', custom_audio: 'Генерирует звук',
      p1: 'Креативная схема', p2: 'Раскадровка', p3: 'Основной промпт видео', p4: 'Промпты по сценам', p5: 'Персонаж', p6: 'Окружение', p7: 'Камера и движение', p8: 'Звук', p9: 'Негативный промпт', p10: 'Три адаптации', p11: 'Чек-лист производства', prov: 'Взято из исходника и предложено ИИ',
      from_src: 'Взято из исходника', by_ai: 'Предложено ИИ', shot: 'Сцена', continuity: 'Связность', up_to_chars: 'до {n} символов',
      lib_empty: 'Библиотека пуста. Сохраните видео из радара или загрузите его для разбора.', prod: 'Производство', notes: 'Заметки', tags: 'Теги через запятую', saveb: 'Сохранить', metrics_hist: 'Замеры', add_meas: 'Добавить замер', refresh_src: 'Обновить из источника', pubs: 'Мои публикации', add_pub: 'Добавить публикацию', pub_link: 'Ссылка на мой пост',
      exp_md: 'Экспорт · Markdown', exp_json: 'Экспорт · JSON', del: 'Удалить', confirm_del: 'Удалить видео вместе с разборами и промптами?', anomaly: 'Индекс аномалии', missing: 'Не учтено — нет данных', prompts: 'Наборы промптов', analyses: 'Разборы', weight: 'вес',
      disclaimer: 'Сигнал для сортировки по метрикам выше. Это не вероятность вирусности.', when: 'Когда', src_col: 'Источник',
      loading: 'Загрузка…', err: 'Ошибка', done: 'Готово', started: 'Запущено', refs_n: 'видео', found: 'Найдено {n}, новых {m}', added_meta: 'Добавлено. Метрики популярности по ссылке недоступны — их можно внести вручную.',
      st_ready: 'работает', st_needs_key: 'доступ не настроен', st_metadata_only: 'только по ссылке', st_not_connected: 'не подключено', st_error: 'ошибка запроса',
      cov_title: 'Что подключено', cov_search: 'Поиск трендов', cov_metrics: 'Метрики популярности', cov_meta: 'Данные по ссылке', cov_note: 'Ничего не имитируется. Платформы без подключения можно заполнять по ссылке, импортом таблицы или вручную. Подключения настраивает администратор сервера.',
      cap_teardown: 'Разбор видео', cap_speech: 'Расшифровка речи', cap_on: 'работает', cap_off: 'не подключена',
      no_metrics_n: 'без метрик популярности — для них индекс аномалии недоступен.', search_blocked: 'Поиск по этой платформе пока не подключён', retry_later: 'Повторить',
      credits: 'У AI-провайдера закончились кредиты. Ничего не сгенерировано — повторите после пополнения баланса.',
      st_queued: 'в очереди', st_processing: 'в работе', st_completed: 'готово', st_failed: 'ошибка', writing: 'Модель пишет набор промптов…', analysing: 'Видео разбирается…', nothing_gen: 'Ничего не сгенерировано.',
      reused_an: 'Этот файл уже разбирали — показан сохранённый разбор.', reused_pr: 'Набор с такими настройками уже есть — показан он. Для другого варианта нажмите «Написать новую версию».', need_analysis: 'У этого видео ещё нет готового разбора — сделайте его на вкладке «Разбор видео».', pick_ref_first: 'Сначала выберите видео',
      social_note: 'Если платформа не даёт ARES скачать видео, сохраните свою копию и загрузите её здесь. Одна обложка разбором не считается.',
      inj: 'В тексте видео встретились фразы, похожие на команды. Они учтены только как данные и не выполнялись.',
      imp_note: 'CSV со строкой заголовков или JSON-список. Пустые ячейки остаются пустыми — ничего не досчитывается.', imported: 'Импортировано: {n}', imp_errors: 'строк с ошибками: {n}',
      f_len: 'Длина и формат', f_audio: 'Звук', f_edit: 'Монтаж', f_text: 'Расшифровка речи', f_frames: 'Проанализировано кадров', f_none: 'без звука', f_yes: 'есть', f_pauses: 'пауз: {n}',
      f_cuts: 'склеек: {n} · средний план {s} с', f_chars: '{n} символов', f_tr_off: 'недоступна — расшифровка не подключена',
      b_observed: 'видно в кадре', b_inferred: 'предположение', b_unknown: 'нет данных',
      e_generic: 'Что-то пошло не так. Повторите попытку.', e_http_402: 'У AI-провайдера закончились кредиты. Ничего не сгенерировано — повторите после пополнения баланса.',
      e_llm_not_configured: 'AI-модель не настроена. Обратитесь к администратору.', e_llm: 'AI-модель не ответила корректно. Повторите позже.', e_media: 'Сервер не смог обработать это видео. Повторите позже.',
      e_file_too_large: 'Файл слишком большой.', e_too_long: 'Видео слишком длинное.', e_bad_file: 'Файл не похож на поддерживаемое видео.', e_upload_missing: 'Исходный файл уже удалён — загрузите его снова.',
      e_no_search: 'Поиск по этой платформе пока не подключён.', e_analysis_required: 'Сначала сделайте разбор этого видео.', e_not_found: 'Не найдено.', e_no_url: 'Вставьте ссылку.', e_invalid: 'Проверьте введённые данные.',
      e_no_records: 'В файле нет записей.', e_not_failed: 'Повторить можно только неудачный запуск.', e_unavailable: 'Модуль временно недоступен.' }
  };
  /* enum and report-key labels (fallback: the key with spaces) */
  const E = {
    en: { youtube: 'YouTube', tiktok: 'TikTok', instagram: 'Instagram', youtube_api: 'YouTube', oembed: 'Link', manual: 'Manual', import: 'Import', upload: 'Upload',
      new: 'new', watching: 'watching', rising: 'rising', saturated: 'saturated', dead: 'faded', idea: 'idea', analysed: 'analysed', in_production: 'in production', published: 'published', archived: 'archived',
      reach: 'Reach', retention: 'Retention', click_through: 'Clicks', engagement: 'Engagement', brand_awareness: 'Awareness',
      generic_t2v: 'Any text-to-video tool', short_clip_chain: 'Short clips, chained', native_audio: 'One shot with generated sound', custom: 'My tool (set limits)',
      relative_views: 'Views vs. the author’s norm', freshness: 'Freshness', velocity: 'Growth speed', repeatability: 'Repeatability' },
    ru: { youtube: 'YouTube', tiktok: 'TikTok', instagram: 'Instagram', youtube_api: 'YouTube', oembed: 'Ссылка', manual: 'Вручную', import: 'Импорт', upload: 'Загрузка',
      new: 'новый', watching: 'наблюдаем', rising: 'растёт', saturated: 'насыщен', dead: 'угас', idea: 'идея', analysed: 'разобрано', in_production: 'в производстве', published: 'опубликовано', archived: 'в архиве',
      reach: 'Охват', retention: 'Удержание', click_through: 'Переходы', engagement: 'Вовлечённость', brand_awareness: 'Узнаваемость',
      generic_t2v: 'Любой генератор «текст → видео»', short_clip_chain: 'Короткие клипы цепочкой', native_audio: 'Один кадр со звуком', custom: 'Свой инструмент (задать лимиты)',
      relative_views: 'Просмотры к норме автора', freshness: 'Свежесть', velocity: 'Скорость роста', repeatability: 'Повторяемость',
      talking_head: 'говорящая голова', pov: 'POV', reaction: 'реакция', dance: 'танец', transition: 'переход', cinematic: 'кинематографичное', storytelling: 'история', prank: 'пранк', challenge: 'челлендж', before_after: 'до/после',
      voice_over: 'закадровый голос', ugc_style: 'UGC', green_screen: 'зелёный экран', transformation: 'трансформация', short_sketch: 'скетч',
      direct_to_camera: 'в камеру', text_on_screen: 'текст на экране', music_only: 'только музыка', dialogue: 'диалог', no_face: 'без лица', screen_recording: 'запись экрана', mixed: 'смешанная',
      topic: 'Тема', format: 'Формат', goal: 'Цель', audience: 'Аудитория', strength: 'Сильная сторона', appearance: 'Внешность', age_range: 'Возраст', clothing: 'Одежда', pose: 'Поза', gaze: 'Взгляд', expression: 'Мимика',
      gestures: 'Жесты', movement_sequence: 'Последовательность движений', choreography_rhythm: 'Ритм движений', shot_types: 'Планы', camera_movement: 'Движение камеры', distance_changes: 'Смена дистанции', speed: 'Темп',
      stabilisation: 'Стабилизация', composition: 'Композиция', scene_depth: 'Глубина сцены', lighting: 'Свет', environment_style: 'Стиль окружения', color_grade: 'Цветокоррекция', speech: 'Речь', transcript_summary: 'О чём говорят',
      sound_effects: 'Звуковые эффекты', music_mood_tempo: 'Музыка: настроение и темп', rhythm_sync: 'Синхронизация с ритмом', pauses_key_sounds: 'Паузы и ключевые звуки', hook: 'Хук', development: 'Развитие',
      visual_stimulus_changes: 'Смена визуальных стимулов', climax: 'Кульминация', ending: 'Финал', cta: 'Призыв к действию', camera: 'Камера', camera_motion: 'Движение камеры', on_screen_text: 'Текст в кадре', sound: 'Звук',
      setting: 'Место', props: 'Реквизит', mood: 'Настроение', palette: 'Палитра', time_of_day: 'Время суток', wardrobe: 'Гардероб', look: 'Образ', age: 'Возраст', lens: 'Объектив', movement: 'Движение', music: 'Музыка', sfx: 'Эффекты', voice: 'Голос', hook_line: 'Хук', concept: 'Идея', platform: 'Платформа', duration: 'Длина' }
  };
  const T = (k, v) => { let s = (D[L()] && D[L()][k]) || D.en[k] || k; if (v) Object.keys(v).forEach(n => { s = s.split('{' + n + '}').join(v[n]); }); return s; };
  const N = k => k == null || k === '' ? '' : (E[L()] && E[L()][k]) || E.en[k] || String(k).replace(/_/g, ' ');
  /* backend error code (also "code: message" strings stored on failed runs) -> plain language */
  const ERR = { http_402: 'e_http_402', llm_not_configured: 'e_llm_not_configured', llm_unreachable: 'e_llm', llm_failed: 'e_llm', empty_response: 'e_llm', invalid_json: 'e_llm',
    ffmpeg_missing: 'e_media', ffmpeg_timeout: 'e_media', frame_failed: 'e_media', audio_failed: 'e_media', file_too_large: 'e_file_too_large', body_too_large: 'e_file_too_large', too_long: 'e_too_long',
    unsupported_format: 'e_bad_file', not_a_video: 'e_bad_file', unreadable_video: 'e_bad_file', empty_file: 'e_bad_file', upload_missing: 'e_upload_missing', needs_key: 'e_no_search', no_search_source: 'e_no_search',
    no_metrics_source: 'e_no_search', analysis_required: 'e_analysis_required', not_found: 'e_not_found', no_url: 'e_no_url', invalid_field: 'e_invalid', invalid_filter: 'e_invalid', invalid_metrics: 'e_invalid',
    invalid_params: 'e_invalid', no_records: 'e_no_records', not_failed: 'e_not_failed', creative_unavailable: 'e_unavailable' };
  const errText = x => { const code = typeof x === 'string' ? x.split(':')[0].trim() : (x && x.code) || ''; return T(ERR[code] || 'e_generic'); };

  const S = { tab: 'radar', status: null, refs: { items: [], total: 0 }, libRefs: { items: [], total: 0 }, filters: {}, libFilters: {}, anRef: null, fgRef: null, fgPrompt: null, polls: {}, root: null, last: {} };

  /* ---------- helpers ---------- */
  function h(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const k in (attrs || {})) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v; else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (k === 'value') e.value = v; else if (k === 'checked' || k === 'disabled' || k === 'selected' || k === 'multiple') e[k] = !!v; else e.setAttribute(k, v === true ? '' : v);
    }
    for (const c of kids.flat()) if (c != null && c !== false) e.append(c.nodeType ? c : document.createTextNode(String(c)));
    return e;
  }
  const clear = n => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
  const fmt = n => n == null ? null : n >= 1e6 ? (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + 'K' : String(n);
  const num = n => n == null ? '—' : Number(n).toLocaleString(L() === 'ru' ? 'ru-RU' : 'en-GB');
  const dur = s => s == null ? '' : Math.floor(s / 60) + ':' + String(Math.round(s % 60)).padStart(2, '0');
  const dt = s => s ? new Date(s).toLocaleString(L() === 'ru' ? 'ru-RU' : 'en-GB', { dateStyle: 'medium', timeStyle: 'short' }) : '—';
  const safeUrl = u => /^https?:\/\//i.test(u || '') ? u : null;
  function toast(msg, bad) { const t = h('div', { class: 'cr-toast' + (bad ? ' bad' : ''), role: 'status' }, msg); document.body.append(t); setTimeout(() => t.classList.add('out'), bad ? 5600 : 2200); setTimeout(() => t.remove(), bad ? 6000 : 2600); }
  async function api(path, opts) {
    opts = opts || {};
    if (opts.json !== undefined) { opts.method = opts.method || 'POST'; opts.headers = { 'Content-Type': 'application/json' }; opts.body = JSON.stringify(opts.json); delete opts.json; }
    const r = await P().authFetch('/api/creative' + path, opts);
    let d = null; try { d = await r.json(); } catch (e) {}
    if (!r.ok) { const det = (d && (d.detail || d)) || {}; const e = new Error(''); e.code = det.error || ''; e.message = errText(e); throw e; }
    return d;
  }
  async function copy(text, btn) {
    try { await navigator.clipboard.writeText(text); } catch (e) { const ta = h('textarea', { style: 'position:fixed;opacity:0' }); ta.value = text; document.body.append(ta); ta.select(); try { document.execCommand('copy'); } catch (e2) {} ta.remove(); }
    if (btn) { const o = btn.textContent; btn.textContent = T('copied'); setTimeout(() => btn.textContent = o, 1200); }
  }
  function field(label, input, cls) { return h('label', { class: 'cr-f ' + (cls || '') }, h('span', null, label), input); }
  const sel = (opts, val, id) => { const s = h('select', { id }); opts.forEach(o => s.append(h('option', { value: o[0], selected: String(o[0]) === String(val) }, o[1]))); return s; };
  const inp = (id, ph, val, type) => h('input', { id, placeholder: ph || '', value: val == null ? '' : val, type: type || 'text' });
  const PLAT = () => [['', T('any')], ['youtube', 'YouTube Shorts'], ['tiktok', 'TikTok'], ['instagram', 'Instagram Reels']];
  const enumOpts = (arr, any) => [['', any || T('any')]].concat((arr || []).map(x => [x, N(x)]));
  const val = id => { const e = S.root.querySelector('#' + id); return e ? (e.type === 'checkbox' ? e.checked : e.value.trim()) : ''; };
  const stCls = x => x === 'ready' ? 'ok' : x === 'error' ? 'bad' : x === 'not_connected' ? '' : 'warn';
  const stat = s => h('span', { class: 'cr-st ' + stCls(s) }, h('i'), T('st_' + s));
  const status = s => h('span', { class: 'cr-status ' + s }, T('st_' + s));

  /* ---------- shell ---------- */
  function render() {
    const root = clear(S.root);
    const tabs = h('div', { class: 'cr-tabs', role: 'tablist' });
    [['radar', 'tab_radar'], ['analysis', 'tab_analysis'], ['forge', 'tab_forge'], ['lib', 'tab_lib']].forEach(([k, l], i) => tabs.append(h('button', { class: 'cr-tab' + (S.tab === k ? ' on' : ''), role: 'tab', 'aria-selected': S.tab === k, onclick: () => { S.tab = k; render(); } }, h('small', null, String(i + 1).padStart(2, '0')), T(l))));
    root.append(tabs);
    const body = h('div', { id: 'crBody', class: 'cr-body-in' });
    root.append(body);
    ({ radar: renderRadar, analysis: renderAnalysis, forge: renderForge, lib: renderLib })[S.tab](body);
  }

  /* ---------- reference cards ---------- */
  function card(r, opts) {
    const m = r.metrics, mv = (k, label) => m[k] != null ? h('span', null, h('b', null, fmt(m[k])), ' ' + T(label)) : h('span', { class: 'na' }, '— ' + T(label));
    const u = safeUrl(r.thumbnail_url);
    const thumb = h('button', { class: 'cr-thumb', type: 'button', onclick: () => openDetail(r.id), 'aria-label': T('details') + ': ' + (r.title || '') },
      u ? h('img', { src: u, alt: '', loading: 'lazy', referrerpolicy: 'no-referrer', onerror: ev => ev.target.remove() }) : h('span', { class: 'cr-ph' }, N(r.platform)),
      h('span', { class: 'cr-plat' }, N(r.platform)),
      h('span', { class: 'cr-score' + (r.anomaly.score == null ? ' none' : ''), title: T('score_tip') }, r.anomaly.score == null ? T('no_score') : String(r.anomaly.score)),
      r.duration_s ? h('span', { class: 'cr-dur' }, dur(r.duration_s)) : null);
    const tags = h('div', { class: 'cr-tags' }, r.video_type ? h('span', { class: 'cr-tag' }, N(r.video_type)) : null, r.niche ? h('span', { class: 'cr-tag' }, r.niche) : null, r.trend_status && r.trend_status !== 'new' ? h('span', { class: 'cr-tag' }, N(r.trend_status)) : null);
    const src = safeUrl(r.url);
    return h('article', { class: 'cr-card' }, thumb,
      h('div', { class: 'cr-body' }, h('div', { class: 'cr-title' }, r.title || r.url || '—'),
        h('div', { class: 'cr-sub' }, (r.author || '—') + (m.followers != null ? ' · ' + fmt(m.followers) + ' ' + T('followers') : '') + (r.published_at ? ' · ' + dt(r.published_at) : '')),
        h('div', { class: 'cr-metrics' }, mv('views', 'views'), mv('likes', 'likes'), mv('comments', 'comments')), tags),
      h('div', { class: 'cr-actions' },
        h('button', { class: 'cr-btn sm' + (r.saved ? ' on' : ''), onclick: async () => { try { await api('/refs/' + r.id, { method: 'PATCH', json: { saved: !r.saved } }); toast(r.saved ? T('unsaved') : T('saved')); opts.reload(); } catch (e) { toast(e.message, true); } } }, r.saved ? '★ ' + T('saved') : '☆ ' + T('save')),
        h('button', { class: 'cr-btn sm', onclick: () => { S.anRef = r.id; S.tab = 'analysis'; render(); } }, T('analyze')),
        src ? h('a', { class: 'cr-btn sm ghost', href: src, target: '_blank', rel: 'noopener noreferrer' }, T('open_src') + ' ↗') : null));
  }
  function grid(data, opts, emptyMsg) {
    if (!data.items.length) return h('div', { class: 'cr-empty' }, h('b', null, '✳'), emptyMsg);
    const nm = data.items.filter(r => r.metrics.views == null).length;
    return h('div', null, h('div', { class: 'cr-count' }, h('b', null, String(data.total)), ' ' + T('refs_n') + (nm ? ' · ' + nm + ' / ' + data.items.length + ' ' + T('no_metrics_n') : '')), h('div', { class: 'cr-grid' }, data.items.map(r => card(r, opts))));
  }

  /* ---------- Trend radar ---------- */
  function coverage() {
    const st = S.status; if (!st || !st.coverage) return null;
    return h('section', { class: 'cr-sec-open' }, h('h3', null, T('cov_title')),
      h('div', { class: 'cr-cov' },
        h('div', { class: 'cr-cov-row head' }, h('span', null, T('platform')), h('span', null, T('cov_search')), h('span', null, T('cov_metrics')), h('span', null, T('cov_meta'))),
        Object.entries(st.coverage).map(([pl, row]) => h('div', { class: 'cr-cov-row' }, h('span', { class: 'cr-cov-pl' }, N(pl)), [['search', 'cov_search'], ['metrics', 'cov_metrics'], ['metadata', 'cov_meta']].map(([k, l]) => h('span', { 'data-l': T(l) }, stat(row[k].state)))))),
      h('div', { class: 'cr-caps' },
        h('span', null, T('cap_teardown') + ' — ', stat(st.vision.image_input && st.ffmpeg.available ? 'ready' : 'not_connected')),
        h('span', null, T('cap_speech') + ' — ', h('span', { class: 'cr-st ' + (st.transcription.available ? 'ok' : '') }, h('i'), T(st.transcription.available ? 'cap_on' : 'cap_off')))),
      h('p', { class: 'cr-note' }, T('cov_note')));
  }
  function renderRadar(body) {
    const st = S.status, f = S.filters;
    const srcOpts = [['', T('auto')]].concat((st ? st.sources : []).filter(s => s.capabilities.includes('search')).map(s => [s.id, N(s.id) + (s.state === 'ready' ? '' : ' · ' + T('st_' + s.state))]));
    const addPanel = h('section', { class: 'cr-add' },
      h('label', { class: 'cr-add-f' }, h('span', null, T('add_url')), h('input', { id: 'aUrl', placeholder: 'https://…', autocomplete: 'off' })),
      h('input', { id: 'aNiche', placeholder: T('niche'), class: 'cr-add-n' }),
      h('button', { class: 'cr-btn primary', onclick: addByUrl }, T('add')), h('button', { class: 'cr-btn ghost', onclick: openImport }, T('import')),
      h('div', { class: 'cr-note', id: 'aMsg' }));
    const searchPanel = h('section', { class: 'cr-sec-open' }, h('h3', null, T('search_src')),
      h('div', { class: 'cr-form' },
        field(T('platform'), sel(PLAT().slice(1), f.s_platform || 'youtube', 'sPlat')), field(T('keywords'), inp('sQ', '', f.s_q), 'span2'), field(T('niche'), inp('sNiche', '', f.s_niche)),
        field(T('geo'), inp('sGeo', 'US, MX…', f.s_geo)), field(T('language'), inp('sLang', 'en, es…', f.s_lang)), field(T('within'), inp('sDays', '', f.s_days || '30', 'number')),
        field(T('maxdur'), inp('sDur', '', f.s_dur || '60', 'number')), field(T('order'), sel([['views', T('by_views')], ['date', T('by_date')]], f.s_order, 'sOrder')), field(T('source'), sel(srcOpts, f.s_src, 'sSrc')),
        h('button', { class: 'cr-btn primary', id: 'sGo', onclick: runSearch }, T('search'))),
      h('div', { class: 'cr-note', id: 'sErr' }));
    const gate = () => { const c = st && st.coverage && st.coverage[val('sPlat') || 'youtube']; const ok = !c || c.search.state === 'ready' || c.search.state === 'error'; const b = S.root.querySelector('#sGo'); if (b) b.disabled = !ok; const e = S.root.querySelector('#sErr'); if (e) { e.className = 'cr-note' + (ok ? '' : ' warn'); e.textContent = ok ? '' : T('search_blocked') + ' (' + T('st_' + c.search.state) + ').'; } };
    searchPanel.querySelector('#sPlat').addEventListener('change', gate); setTimeout(gate, 0);
    const filt = h('details', { class: 'cr-sec-open cr-filters', open: Object.values(f).some(Boolean) && Object.keys(f).some(k => !k.startsWith('s_') && f[k]) },
      h('summary', null, h('h3', null, T('filters'))),
      h('div', { class: 'cr-form' },
        field(T('platform'), sel(PLAT(), f.platform, 'fPlat')), field(T('keywords'), inp('fQ', '', f.q)), field(T('niche'), inp('fNiche', '', f.niche)), field(T('geo'), inp('fGeo', '', f.geo)), field(T('language'), inp('fLang', '', f.language)),
        field(T('minv'), inp('fMinV', '', f.min_views, 'number')), field(T('maxv'), inp('fMaxV', '', f.max_views, 'number')), field(T('minf'), inp('fMinF', '', f.min_followers, 'number')),
        field(T('within'), inp('fPub', '', f.published_within_days, 'number')), field(T('disc'), inp('fDisc', '', f.discovered_within_days, 'number')), field(T('maxdur'), inp('fDur', '', f.max_duration, 'number')),
        field(T('vtype'), sel(enumOpts(st && st.enums.video_types), f.video_type, 'fType')), field(T('delivery'), sel(enumOpts(st && st.enums.delivery), f.delivery, 'fDel')), field(T('tstatus'), sel(enumOpts(st && st.enums.trend_statuses), f.trend_status, 'fTS')),
        field(T('sort'), sel([['anomaly', T('s_anomaly')], ['views', T('s_views')], ['engagement', T('s_eng')], ['date', T('s_date')], ['discovered', T('s_disc')]], f.sort, 'fSort')),
        h('button', { class: 'cr-btn', onclick: () => { readFilters(); loadRadar(); } }, T('apply'))));
    body.append(addPanel, h('div', { id: 'crGrid' }, h('div', { class: 'cr-note' }, T('loading'))), filt, searchPanel, coverage());
    loadRadar();
  }
  function readFilters() {
    const m = { platform: 'fPlat', q: 'fQ', niche: 'fNiche', geo: 'fGeo', language: 'fLang', min_views: 'fMinV', max_views: 'fMaxV', min_followers: 'fMinF', published_within_days: 'fPub', discovered_within_days: 'fDisc', max_duration: 'fDur', video_type: 'fType', delivery: 'fDel', trend_status: 'fTS', sort: 'fSort' };
    S.filters = { ...S.filters };
    for (const k in m) S.filters[k] = val(m[k]);
    ['s_platform:sPlat', 's_q:sQ', 's_niche:sNiche', 's_geo:sGeo', 's_lang:sLang', 's_days:sDays', 's_dur:sDur', 's_order:sOrder', 's_src:sSrc'].forEach(p => { const [k, id] = p.split(':'); S.filters[k] = val(id); });
  }
  async function loadRadar() {
    const qs = new URLSearchParams(); const f = S.filters;
    ['platform', 'q', 'niche', 'geo', 'language', 'min_views', 'max_views', 'min_followers', 'published_within_days', 'discovered_within_days', 'max_duration', 'video_type', 'delivery', 'trend_status', 'sort'].forEach(k => f[k] && qs.set(k, f[k]));
    try { S.refs = (await api('/refs?' + qs)); } catch (e) { return fail('crGrid', e); }
    const g = S.root.querySelector('#crGrid'); if (g) clear(g).append(grid(S.refs, { reload: loadRadar }, T('none_found')));
  }
  function fail(id, e) { const g = S.root.querySelector('#' + id); if (g) clear(g).append(h('div', { class: 'cr-note bad' }, e.message || T('e_generic'))); }
  async function runSearch() {
    readFilters(); const f = S.filters, b = S.root.querySelector('#sGo'); b.disabled = true;
    try {
      const r = await api('/search', { json: { platform: f.s_platform, query: f.s_q, niche: f.s_niche, geo: f.s_geo, language: f.s_lang, published_within_days: +f.s_days || null, max_duration_s: +f.s_dur || null, order: f.s_order, source: f.s_src || undefined } });
      toast(T('found', { n: r.found, m: r.new })); S.filters.sort = S.filters.sort || 'anomaly'; await loadRadar();
    } catch (e) { const er = S.root.querySelector('#sErr'); if (er) { er.className = 'cr-note bad'; er.textContent = e.message; } toast(e.message, true); } finally { b.disabled = false; }
  }
  async function addByUrl() {
    const url = val('aUrl'); if (!url) return; const m = S.root.querySelector('#aMsg'); m.className = 'cr-note'; m.textContent = '…';
    try { const r = await api('/refs', { json: { url, niche: val('aNiche'), save: true } }); m.textContent = (r.warnings || []).length ? T('added_meta') : T('done'); S.root.querySelector('#aUrl').value = ''; await loadRadar(); }
    catch (e) { m.textContent = e.message; m.className = 'cr-note bad'; }
  }
  function openImport() {
    const ta = h('textarea', { id: 'impTa', placeholder: 'url,title,author,views,likes,comments,shares,followers,published_at,niche,geo,language,video_type\nhttps://www.youtube.com/shorts/…,My title,Author,120000,5000,200,,34000,2026-10-01,fitness,US,en,pov', class: 'cr-mono' });
    const out = h('div', { class: 'cr-note' });
    showDrawer(T('import'), [h('p', { class: 'cr-note' }, T('imp_note')), ta,
      h('div', { class: 'cr-row' }, h('button', { class: 'cr-btn primary', onclick: async () => {
        const txt = ta.value.trim(); if (!txt) return;
        let body; try { body = txt.startsWith('[') || txt.startsWith('{') ? { records: [].concat(JSON.parse(txt)) } : { csv: txt }; } catch (e) { out.textContent = T('e_invalid'); return; }
        try { const r = await api('/refs/import', { json: { ...body, save: false } });
          out.textContent = T('imported', { n: r.imported }) + (r.errors.length ? ' · ' + T('imp_errors', { n: r.errors.length }) + ' (#' + r.errors.map(x => x.row).join(', #') + ')' : ''); loadRadar(); } catch (e) { out.textContent = e.message; } } }, T('import'))), out]);
  }

  /* ---------- drawer & detail ---------- */
  function showDrawer(title, kids) {
    closeDrawer();
    const d = h('div', { class: 'cr-modal', id: 'crDrawer', onclick: ev => { if (ev.target.id === 'crDrawer') closeDrawer(); } },
      h('div', { class: 'cr-drawer', role: 'dialog', 'aria-label': title }, h('div', { class: 'cr-drawer-h' }, h('h2', null, title), h('button', { class: 'cr-btn sm ghost', onclick: closeDrawer, 'aria-label': 'Close' }, '✕')), kids));
    S.root.append(d);
    const esc = ev => { if (ev.key === 'Escape') { closeDrawer(); document.removeEventListener('keydown', esc); } };
    document.addEventListener('keydown', esc);
  }
  const closeDrawer = () => { const d = document.getElementById('crDrawer'); if (d) d.remove(); };
  async function openDetail(id) {
    let r; try { r = (await api('/refs/' + id)).ref; } catch (e) { return toast(e.message, true); }
    const st = S.status.enums, a = r.anomaly, src = safeUrl(r.url);
    const comp = Object.entries(a.components).map(([k, c]) => h('div', { class: 'cr-comp' }, h('span', null, N(k)), h('div', { class: 'cr-bar' }, h('i', { style: 'width:' + Math.round(c.norm * 100) + '%' })), h('small', null, T('weight') + ' ' + Math.round(c.weight * 100) + '%')));
    const MK = ['views', 'likes', 'comments', 'shares', 'followers'];
    const hist = r.metric_history.map(m => h('tr', null, h('td', null, dt(m.captured_at)), MK.map(k => h('td', { class: 'n' }, m[k] == null ? '—' : num(m[k]))), h('td', null, N(m.source))));
    const edit = h('div', { class: 'cr-form wide' },
      field(T('niche'), inp('eNiche', '', r.niche)), field(T('geo'), inp('eGeo', '', r.geo)), field(T('language'), inp('eLang', '', r.language)),
      field(T('vtype'), sel(enumOpts(st.video_types), r.video_type, 'eType')), field(T('delivery'), sel(enumOpts(st.delivery), r.delivery, 'eDel')),
      field(T('tstatus'), sel(st.trend_statuses.map(x => [x, N(x)]), r.trend_status, 'eTS')), field(T('prod'), sel(st.production_statuses.map(x => [x, N(x)]), r.production_status, 'eProd')),
      field(T('tags'), inp('eTags', '', r.tags.join(', ')), 'full'), field(T('notes'), h('textarea', { id: 'eNotes' }, r.notes || ''), 'full'));
    edit.querySelector('#eNotes').value = r.notes || '';
    const save = h('button', { class: 'cr-btn primary', onclick: async () => {
      const g = id2 => document.querySelector('#crDrawer #' + id2).value.trim();
      try { await api('/refs/' + id, { method: 'PATCH', json: { niche: g('eNiche'), geo: g('eGeo'), language: g('eLang'), video_type: g('eType'), delivery: g('eDel'), trend_status: g('eTS'), production_status: g('eProd'), tags: g('eTags').split(',').map(x => x.trim()).filter(Boolean), notes: g('eNotes'), saved: true } }); toast(T('done')); reloadCurrent(); } catch (e) { toast(e.message, true); } } }, T('saveb'));
    const meas = MK.map(k => h('input', { id: 'm_' + k, placeholder: T(k), type: 'number', min: 0 }));
    const pub = h('div', { class: 'cr-row' }, h('input', { id: 'pUrl', placeholder: T('pub_link'), class: 'grow' }), ['views', 'likes', 'comments', 'shares'].map(k => h('input', { id: 'p_' + k, placeholder: T(k), type: 'number', min: 0, class: 'num' })),
      h('button', { class: 'cr-btn sm', onclick: async () => { const g = i => document.querySelector('#crDrawer #' + i).value; try { await api('/refs/' + id + '/publications', { json: { url: g('pUrl'), views: g('p_views'), likes: g('p_likes'), comments: g('p_comments'), shares: g('p_shares'), posted_at: new Date().toISOString() } }); toast(T('done')); openDetail(id); } catch (e) { toast(e.message, true); } } }, T('add_pub')));
    showDrawer(r.title || r.url || '—', [
      h('div', { class: 'cr-meta' }, [N(r.platform), r.author || '—', dt(r.published_at)].join(' · ')),
      h('div', { class: 'cr-row' }, src ? h('a', { class: 'cr-btn sm ghost', href: src, target: '_blank', rel: 'noopener noreferrer' }, T('open_src') + ' ↗') : null,
        h('button', { class: 'cr-btn sm', onclick: () => { closeDrawer(); S.anRef = id; S.tab = 'analysis'; render(); } }, T('analyze')),
        h('button', { class: 'cr-btn sm', onclick: () => { closeDrawer(); S.fgRef = id; S.tab = 'forge'; render(); } }, T('tab_forge')),
        h('button', { class: 'cr-btn sm ghost', onclick: () => download(id, 'md') }, T('exp_md')), h('button', { class: 'cr-btn sm ghost', onclick: () => download(id, 'json') }, T('exp_json')),
        r.url ? h('button', { class: 'cr-btn sm ghost', onclick: async () => { try { await api('/refs/' + id + '/refresh', { method: 'POST' }); toast(T('done')); openDetail(id); } catch (e) { toast(e.message, true); } } }, T('refresh_src')) : null,
        h('button', { class: 'cr-btn sm danger', onclick: async () => { if (confirm(T('confirm_del'))) { try { await api('/refs/' + id, { method: 'DELETE' }); closeDrawer(); reloadCurrent(); } catch (e) { toast(e.message, true); } } } }, T('del'))),
      h('section', { class: 'cr-dsec' }, h('h3', null, T('anomaly')), a.score != null ? h('div', { class: 'cr-big' }, String(a.score), h('small', null, '/ 100')) : h('p', { class: 'cr-note' }, T('no_score')),
        comp, Object.keys(a.missing).length ? h('p', { class: 'cr-note' }, T('missing') + ': ' + Object.keys(a.missing).map(N).join(', ')) : null, h('p', { class: 'cr-note' }, T('disclaimer'))),
      h('section', { class: 'cr-dsec' }, h('h3', null, T('metrics_hist')), r.metric_history.length ? h('div', { class: 'cr-tw' }, h('table', { class: 'cr-t' }, h('thead', null, h('tr', null, h('th', null, T('when')), MK.map(k => h('th', { class: 'n' }, T(k))), h('th', null, T('src_col')))), h('tbody', null, hist))) : h('p', { class: 'cr-note' }, '—'),
        h('div', { class: 'cr-row' }, meas, h('button', { class: 'cr-btn sm', onclick: async () => { const body = {}; MK.forEach(k => { const v = document.querySelector('#crDrawer #m_' + k).value; if (v !== '') body[k] = v; }); try { await api('/refs/' + id + '/metrics', { json: body }); toast(T('done')); openDetail(id); } catch (e) { toast(e.message, true); } } }, T('add_meas')))),
      h('section', { class: 'cr-dsec' }, edit, h('div', { class: 'cr-row' }, save)),
      h('section', { class: 'cr-dsec' }, h('h3', null, T('pubs')), r.publications.map(p => h('div', { class: 'cr-note' }, dt(p.posted_at) + (p.url ? ' · ' + p.url : '') + ' · ' + ['views', 'likes', 'comments', 'shares'].map(k => (p[k] == null ? '—' : num(p[k])) + ' ' + T(k)).join(' · '))), pub),
      h('section', { class: 'cr-dsec' }, h('h3', null, T('analyses') + ' · ' + T('prompts')),
        r.analyses.map(x => h('div', { class: 'cr-row' }, status(x.status), h('span', { class: 'cr-note' }, T('report') + ' · ' + dt(x.created_at)))),
        r.prompts.map(x => h('div', { class: 'cr-row' }, status(x.status), h('button', { class: 'cr-btn sm ghost', onclick: () => { closeDrawer(); S.fgRef = id; S.fgPrompt = x.id; S.tab = 'forge'; render(); } }, N(x.params.generator) + ' · ' + dt(x.created_at))))),
    ]);
  }
  async function download(id, fmt) {
    try { const r = await P().authFetch('/api/creative/refs/' + id + '/export?format=' + fmt); if (!r.ok) throw new Error(T('e_generic')); const b = await r.blob(); const a = h('a', { href: URL.createObjectURL(b), download: 'creative_' + id + '.' + fmt }); document.body.append(a); a.click(); a.remove(); } catch (e) { toast(e.message, true); }
  }
  function reloadCurrent() { if (S.tab === 'radar') loadRadar(); else if (S.tab === 'lib') loadLib(); }

  /* ---------- Library ---------- */
  function renderLib(body) {
    const f = S.libFilters, st = S.status;
    body.append(h('section', { class: 'cr-sec-open' }, h('div', { class: 'cr-form' }, field(T('keywords'), inp('lQ', '', f.q)), field(T('niche'), inp('lNiche', '', f.niche)), field(T('prod'), sel(enumOpts(st && st.enums.production_statuses), f.production_status, 'lProd')),
      field(T('tstatus'), sel(enumOpts(st && st.enums.trend_statuses), f.trend_status, 'lTS')), h('button', { class: 'cr-btn', onclick: () => { S.libFilters = { q: val('lQ'), niche: val('lNiche'), production_status: val('lProd'), trend_status: val('lTS') }; loadLib(); } }, T('apply')))),
      h('div', { id: 'crGrid' }, h('div', { class: 'cr-note' }, T('loading'))));
    loadLib();
  }
  async function loadLib() {
    const qs = new URLSearchParams({ saved: '1', sort: 'discovered' }); Object.entries(S.libFilters).forEach(([k, v]) => v && qs.set(k, v));
    try { S.libRefs = await api('/refs?' + qs); } catch (e) { return fail('crGrid', e); }
    const g = S.root.querySelector('#crGrid'); if (g) clear(g).append(grid(S.libRefs, { reload: loadLib }, T('lib_empty')));
  }

  /* ---------- Video teardown ---------- */
  async function renderAnalysis(body) {
    let refs = []; try { refs = (await api('/refs?limit=200&sort=discovered')).items; } catch (e) {}
    const st = S.status, lim = st.limits;
    const refSel = sel([['', T('new_upload')]].concat(refs.map(r => [r.id, (r.title || r.url || '#' + r.id).slice(0, 70) + ' · ' + N(r.platform)])), S.anRef, 'anRef');
    refSel.addEventListener('change', () => { S.anRef = refSel.value ? +refSel.value : null; loadJobs(); });
    const file = h('input', { type: 'file', id: 'anFile', accept: lim.formats.join(','), style: 'display:none' });
    const dropT = h('b', null, T('drop')), dropS = h('span', null, T('drop_sub'));
    const drop = h('button', { class: 'cr-drop', type: 'button', id: 'anDrop', onclick: () => file.click() }, dropT, dropS);
    file.addEventListener('change', () => { const x = file.files[0]; dropT.textContent = x ? x.name : T('drop'); dropS.textContent = x ? (x.size / 1048576).toFixed(1) + ' MB' : T('drop_sub'); drop.classList.toggle('has', !!x); });
    ['dragover', 'dragenter'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('over'); })); ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', e => { if (e.dataTransfer.files[0]) { file.files = e.dataTransfer.files; file.dispatchEvent(new Event('change')); } });
    const ready = st.vision.image_input && st.ffmpeg.available;
    const fmts = lim.formats.map(x => x.replace('.', '').toUpperCase()).join(', ');
    body.append(h('div', { class: 'cr-split' },
      h('section', { class: 'cr-sec-open' }, h('h3', null, T('upload_title')), drop, file,
        h('p', { class: 'cr-note' }, T('limits', { mb: lim.max_upload_mb, min: Math.round(lim.max_duration_s / 60 * 10) / 10, fmt: fmts })),
        field(T('pick_ref'), refSel), field(T('niche'), inp('anNiche', '')),
        ready ? null : h('p', { class: 'cr-note bad' }, T('cap_teardown') + ': ' + T('st_not_connected') + '. ' + T('e_unavailable')),
        h('button', { class: 'cr-btn primary wide', id: 'anGo', disabled: !ready, onclick: upload }, T('upload_go')),
        h('p', { class: 'cr-note' }, T('social_note'))),
      h('div', null, h('div', { id: 'anJobs' }), h('div', { id: 'anReport' }))));
    loadJobs();
  }
  async function upload() {
    const f = S.root.querySelector('#anFile').files[0]; if (!f) return toast(T('choose_file'), true);
    const fd = new FormData(); fd.append('file', f); if (S.anRef) fd.append('ref_id', S.anRef); fd.append('niche', val('anNiche')); fd.append('language', L());
    const b = S.root.querySelector('#anGo'); b.disabled = true; b.textContent = '…';
    try { const r = await api('/upload', { method: 'POST', body: fd }); S.anRef = r.ref_id; toast(r.reused ? T('reused_an') : T('started')); render(); } catch (e) { toast(e.message, true); } finally { b.disabled = false; b.textContent = T('upload_go'); }
  }
  async function loadJobs() {
    const box = S.root.querySelector('#anJobs'); if (!box) return; clear(box);
    if (!S.anRef) { box.append(h('div', { class: 'cr-empty' }, h('b', null, '✳'), T('drop'))); return; }
    let r; try { r = (await api('/refs/' + S.anRef)).ref; } catch (e) { return box.append(h('div', { class: 'cr-note bad' }, e.message)); }
    box.append(h('section', { class: 'cr-sec-open' }, h('h3', null, T('jobs') + ' · ' + (r.title || '—')), r.analyses.length ? r.analyses.map(jobView) : h('p', { class: 'cr-note' }, '—')));
    const done = r.analyses.find(a => a.status === 'completed');
    if (done) showReport(done, r); else clear(S.root.querySelector('#anReport'));
    if (r.analyses.some(a => a.status === 'queued' || a.status === 'processing')) { clearTimeout(S.polls.an); S.polls.an = setTimeout(() => { if (S.tab === 'analysis' && document.body.contains(box)) loadJobs(); }, 3000); }
  }
  function jobView(a) {
    return h('div', { class: 'cr-job' }, h('div', { class: 'cr-row' }, status(a.status), h('span', { class: 'cr-note' }, dt(a.updated_at)),
      a.status === 'processing' || a.status === 'queued' ? h('span', { class: 'cr-note' }, T('analysing')) : null,
      a.status === 'failed' && a.can_retry ? h('button', { class: 'cr-btn sm', onclick: async () => { try { await api('/analyses/' + a.id + '/retry', { json: { language: L() } }); loadJobs(); } catch (e) { toast(e.message, true); } } }, T('retry')) : null),
      a.status === 'failed' ? h('p', { class: 'cr-note bad' }, a.can_retry ? errText(a.error || '') : T('e_upload_missing')) : null);
  }
  const basisBadge = b => h('span', { class: 'cr-basis ' + (b || 'unknown') }, T('b_' + (b || 'unknown')));
  function kv(obj, skip) { const dl = h('dl', { class: 'cr-kv' }); Object.entries(obj || {}).forEach(([k, v]) => { if (k === 'basis' || (skip || []).includes(k) || v == null || v === '' || (Array.isArray(v) && !v.length)) return; dl.append(h('dt', null, N(k)), h('dd', null, Array.isArray(v) ? v.join(', ') : String(v))); }); return dl; }
  function sec(idx, title, basis, content, extra) { return h('section', { class: 'cr-rep' }, h('header', null, idx ? h('i', null, idx) : null, h('span', null, title), h('span', { class: 'cr-row' }, basis ? basisBadge(basis) : null, extra || null)), h('div', { class: 'cr-rep-b' }, content)); }
  async function showReport(a, ref) {
    const box = clear(S.root.querySelector('#anReport')), r = a.report, f = a.facts || {}, pr = f.probe || {};
    const frames = h('div', { class: 'cr-frames' });
    (a.frames || []).forEach(async fr => { const img = h('img', { alt: '' }); frames.append(h('figure', { class: 'cr-frame' }, img, h('figcaption', null, fr.t.toFixed(1) + ' s'))); try { const resp = await P().authFetch('/api/creative/analyses/' + a.id + '/frames/' + fr.file); if (resp.ok) img.src = URL.createObjectURL(await resp.blob()); } catch (e) {} });
    const tr = f.transcript || {};
    box.append(h('div', { class: 'cr-rep-h' }, h('h3', null, T('report')), h('span', { class: 'cr-note' }, dt(a.updated_at)), h('button', { class: 'cr-btn sm primary', onclick: () => { S.fgRef = ref.id; S.tab = 'forge'; render(); } }, T('to_forge')), h('button', { class: 'cr-btn sm ghost', onclick: () => download(ref.id, 'md') }, T('exp_md'))),
      r.injection_flags && r.injection_flags.length ? h('p', { class: 'cr-note warn' }, T('inj')) : null,
      sec(null, T('facts'), null, h('div', null, h('dl', { class: 'cr-kv' },
        h('dt', null, T('f_len')), h('dd', null, (pr.duration_s || 0).toFixed(1) + ' s · ' + pr.width + '×' + pr.height + (pr.fps ? ' · ' + Math.round(pr.fps) + ' fps' : '')),
        h('dt', null, T('f_audio')), h('dd', null, pr.has_audio ? T('f_yes') + ' · ' + T('f_pauses', { n: (f.silences || []).length }) : T('f_none')),
        h('dt', null, T('f_edit')), h('dd', null, T('f_cuts', { n: f.cut_count, s: f.avg_shot_length_s })),
        h('dt', null, T('f_text')), h('dd', null, tr.available ? T('f_chars', { n: tr.chars }) : T('f_tr_off')),
        h('dt', null, T('f_frames')), h('dd', null, String((a.frames || []).length))), frames)),
      sec('A', T('sec_a'), r.summary.basis, kv(r.summary)),
      sec('B', T('timeline'), null, r.timeline.length ? r.timeline.map(s => h('div', { class: 'cr-scene' }, h('h4', null, h('span', { class: 'cr-tc' }, s.start + '–' + s.end + ' s'), basisBadge(s.basis), s.function ? h('span', { class: 'cr-note' }, s.function) : null), h('div', null, s.action), kv(s, ['start', 'end', 'action', 'function', 'basis']))) : h('p', { class: 'cr-note' }, '—')),
      sec('C', T('sec_c'), r.actor.basis, kv(r.actor)), sec('D', T('sec_d'), r.camera_staging.basis, kv(r.camera_staging)), sec('E', T('sec_e'), r.audio.basis, kv(r.audio)), sec('F', T('sec_f'), r.script_mechanics.basis, kv(r.script_mechanics)),
      sec('G', T('sec_g'), 'inferred', r.why_it_may_work.map(w => h('div', { class: 'cr-hyp' }, h('b', null, w.hypothesis), h('p', { class: 'cr-note' }, w.evidence)))),
      sec(null, T('limits_h'), null, h('ul', { class: 'cr-ul' }, r.limitations.map(x => h('li', null, x)))));
  }

  /* ---------- Prompt studio ---------- */
  async function renderForge(body) {
    let refs = []; try { refs = (await api('/refs?limit=200&sort=discovered')).items; } catch (e) {}
    let presets; try { presets = await api('/presets'); } catch (e) { presets = { presets: {}, goals: [], aspects: [] }; }
    const refSel = sel([['', '—']].concat(refs.slice(0, 60).map(r => [r.id, (r.title || r.url || '#' + r.id).slice(0, 70)])), S.fgRef, 'fgRef');
    const pre = sel(Object.keys(presets.presets).map(k => [k, N(k)]), 'generic_t2v', 'fgGen');
    const custom = h('div', { class: 'cr-form', id: 'fgCustom', style: 'display:none' }, field(T('custom_name'), inp('fgCName', '')), field(T('custom_clip'), inp('fgCClip', '', 8, 'number')), field(T('custom_chars'), inp('fgCChars', '', 2000, 'number')),
      h('label', { class: 'cr-check' }, h('input', { type: 'checkbox', id: 'fgCNeg', checked: true }), T('custom_neg')), h('label', { class: 'cr-check' }, h('input', { type: 'checkbox', id: 'fgCAud' }), T('custom_audio')));
    pre.addEventListener('change', () => custom.style.display = pre.value === 'custom' ? 'grid' : 'none');
    const chk = (id, label, on) => h('label', { class: 'cr-check' }, h('input', { type: 'checkbox', id, checked: on }), label);
    body.append(h('div', { class: 'cr-split' },
      h('section', { class: 'cr-sec-open' }, h('h3', null, T('tab_forge')), field(T('forge_pick'), refSel),
        h('div', { class: 'cr-form' }, field(T('niche'), inp('fgNiche', '')), field(T('audience'), inp('fgAud', '')), field(T('geo'), inp('fgGeo', '')), field(T('language'), inp('fgLang', 'ru, en, es…', L())),
          field(T('platform'), sel(PLAT().slice(1), 'tiktok', 'fgPlat')), field(T('goal'), sel(presets.goals.map(g => [g, N(g)]), 'retention', 'fgGoal')), field(T('duration'), inp('fgDur', '', 15, 'number')), field(T('aspect'), sel(presets.aspects.map(a => [a, a]), '9:16', 'fgAsp')),
          field(T('generator'), pre, 'span2')), custom,
        h('div', { class: 'cr-checks' }, chk('fgChar', T('character'), true), chk('fgVO', T('voiceover'), false), chk('fgTxt', T('onscreen'), true), chk('fgShots', T('shots'), true)),
        field(T('brief'), h('textarea', { id: 'fgBrief' })),
        h('div', { class: 'cr-row' }, h('button', { class: 'cr-btn primary', id: 'fgGo', onclick: () => forgeGo(false) }, T('gen')), h('button', { class: 'cr-btn ghost', onclick: () => forgeGo(true) }, T('regen'))),
        h('div', { class: 'cr-note', id: 'fgMsg' }), h('div', { id: 'fgHist' })),
      h('div', { id: 'fgOut' })));
    refSel.addEventListener('change', () => { S.fgRef = refSel.value ? +refSel.value : null; S.fgPrompt = null; forgeHistory(); });
    forgeHistory();
  }
  async function forgeHistory() {
    const box = S.root.querySelector('#fgHist'); if (!box) return; clear(box);
    const out = S.root.querySelector('#fgOut'); if (out && !S.fgRef) { clear(out).append(h('div', { class: 'cr-empty' }, h('b', null, '✳'), T('pick_ref_first'))); return; }
    try {
      const r = (await api('/refs/' + S.fgRef)).ref;
      const n = r.niche; if (n && !val('fgNiche')) S.root.querySelector('#fgNiche').value = n;
      const msg = S.root.querySelector('#fgMsg'); if (!r.analyses.some(a => a.status === 'completed')) { msg.className = 'cr-note warn'; msg.textContent = T('need_analysis'); } else msg.textContent = '';
      if (r.prompts.length) box.append(h('h4', { class: 'cr-h4' }, T('prompts')), r.prompts.map(p => h('div', { class: 'cr-row' }, status(p.status), h('button', { class: 'cr-btn sm ghost', onclick: () => { S.fgPrompt = p.id; showPrompt(); } }, N(p.params.generator) + ' · ' + dt(p.created_at)))));
      if (S.fgPrompt || (r.prompts[0] && r.prompts[0].status === 'completed')) { S.fgPrompt = S.fgPrompt || r.prompts[0].id; showPrompt(); }
      else if (out) clear(out).append(h('div', { class: 'cr-empty' }, h('b', null, '✳'), T('gen')));
    } catch (e) {}
  }
  async function forgeGo(regen) {
    if (!S.fgRef) return toast(T('pick_ref_first'), true);
    const gen = val('fgGen'), params = { niche: val('fgNiche'), audience: val('fgAud'), geo: val('fgGeo'), language: val('fgLang'), platform: val('fgPlat'), goal: val('fgGoal'), duration_s: +val('fgDur') || 15, aspect: val('fgAsp'), generator: gen,
      character: val('fgChar'), voiceover: val('fgVO'), on_screen_text: val('fgTxt'), separate_shots: val('fgShots'), brief: val('fgBrief') };
    if (gen === 'custom') params.custom = { name: val('fgCName'), max_clip_s: +val('fgCClip') || 8, max_prompt_chars: +val('fgCChars') || 2000, supports_negative: val('fgCNeg'), supports_audio: val('fgCAud') };
    const msg = S.root.querySelector('#fgMsg'); msg.className = 'cr-note'; msg.textContent = '…';
    try { const r = await api('/forge', { json: { ref_id: S.fgRef, params, regenerate: regen } }); S.fgPrompt = r.prompt_id; msg.textContent = r.reused ? T('reused_pr') : T('started'); showPrompt(); }
    catch (e) { msg.className = 'cr-note bad'; msg.textContent = e.message; }
  }
  async function showPrompt() {
    const out = S.root.querySelector('#fgOut'); if (!out || !S.fgPrompt) return;
    let p; try { p = (await api('/prompts/' + S.fgPrompt)).prompt; } catch (e) { return clear(out).append(h('div', { class: 'cr-note bad' }, e.message)); }
    clear(out);
    if (p.status === 'queued' || p.status === 'processing') { out.append(h('div', { class: 'cr-empty' }, status(p.status), T('writing'))); clearTimeout(S.polls.fg); S.polls.fg = setTimeout(() => { if (S.tab === 'forge' && document.body.contains(out)) showPrompt(); }, 3000); return; }
    if (p.status === 'failed') return out.append(h('div', { class: 'cr-empty bad' }, status('failed'), h('p', null, errText(p.error || '')), h('p', { class: 'cr-note' }, T('nothing_gen')),
      h('button', { class: 'cr-btn sm primary', onclick: async () => { try { const r = await api('/forge', { json: { ref_id: p.ref_id, params: p.params, regenerate: true } }); S.fgPrompt = r.prompt_id; showPrompt(); } catch (e) { toast(e.message, true); } } }, T('retry_later'))));
    const x = p.result, cp = (text, label) => h('button', { class: 'cr-btn sm ghost', onclick: ev => copy(text, ev.target) }, label || T('copy'));
    const block = (idx, title, text) => sec(idx, title, null, h('pre', { class: 'cr-pre' }, text), cp(text));
    const kvText = o => Object.entries(o || {}).filter(([k, v]) => k !== 'basis' && v).map(([k, v]) => N(k) + ': ' + v).join('\n');
    const allText = [x.blueprint && kvText(x.blueprint), '— ' + T('p3') + ' —', x.master_prompt, '— ' + T('p9') + ' —', x.negative_prompt].join('\n\n');
    out.append(h('div', { class: 'cr-rep-h' }, h('h3', null, N(x.preset.id)), h('span', { class: 'cr-note' }, T('up_to_chars', { n: x.preset.max_prompt_chars })), cp(allText, T('copy_main')), h('button', { class: 'cr-btn sm ghost', onclick: () => download(p.ref_id, 'md') }, T('exp_md'))),
      x.warnings.length ? h('p', { class: 'cr-note warn' }, x.warnings.join(' · ')) : null,
      sec('01', T('p1'), null, kv(x.blueprint), cp(kvText(x.blueprint))),
      sec('02', T('p2'), null, x.storyboard.map(s => h('div', { class: 'cr-scene' }, h('h4', null, h('span', { class: 'cr-tc' }, '#' + s.n + ' · ' + s.start + '–' + s.end + ' s'), s.purpose ? h('span', { class: 'cr-note' }, s.purpose) : null), h('div', null, s.action), kv(s, ['n', 'start', 'end', 'duration', 'action', 'purpose']))), cp(x.storyboard.map(s => s.n + '. [' + s.start + '-' + s.end + 's] ' + s.action + ' | camera: ' + s.camera_motion + ' | light: ' + s.lighting + ' | text: ' + s.on_screen_text + ' | sound: ' + s.sound).join('\n'))),
      block('03', T('p3'), x.master_prompt),
      sec('04', T('p4'), null, x.shot_prompts.map(s => h('div', { class: 'cr-shot' }, h('div', { class: 'cr-row' }, h('b', null, T('shot') + ' ' + s.n + (s.duration ? ' · ' + s.duration + ' s' : '')), cp(s.prompt)), h('pre', { class: 'cr-pre' }, s.prompt), h('p', { class: 'cr-note' }, T('continuity') + ': ' + s.continuity))), cp(x.shot_prompts.map(s => 'SHOT ' + s.n + ': ' + s.prompt).join('\n\n'), T('copy_all'))),
      x.character ? sec('05', T('p5'), null, kv(x.character), cp(kvText(x.character))) : null,
      sec('06', T('p6'), null, kv(x.environment), cp(kvText(x.environment))), sec('07', T('p7'), null, kv(x.camera_motion), cp(kvText(x.camera_motion))), sec('08', T('p8'), null, kv(x.sound_design), cp(kvText(x.sound_design))),
      block('09', T('p9'), x.negative_prompt),
      sec('10', T('p10'), null, x.adaptations.map(a => h('div', { class: 'cr-hyp' }, h('b', null, a.name), ' ', h('span', { class: 'cr-tag' }, N(a.kind)), h('div', null, a.summary), kv(a, ['name', 'kind', 'summary']))), cp(x.adaptations.map(a => a.name + ' — ' + a.summary + ' | hook: ' + a.hook).join('\n'))),
      sec('11', T('p11'), null, h('ul', { class: 'cr-ul plain' }, x.checklist.map(c => h('li', null, h('label', { class: 'cr-check' }, h('input', { type: 'checkbox' }), c)))), cp(x.checklist.map(c => '- [ ] ' + c).join('\n'))),
      sec(null, T('prov'), null, h('div', { class: 'cr-prov' }, h('div', null, h('h4', null, T('from_src')), h('ul', { class: 'cr-ul' }, x.provenance.confirmed_from_source.map(c => h('li', null, c)))), h('div', null, h('h4', null, T('by_ai')), h('ul', { class: 'cr-ul' }, x.provenance.ai_proposed.map(c => h('li', null, c)))))));
  }

  /* ---------- entry ---------- */
  async function show() {
    if (!P()) return;
    if (!S.root) S.root = document.getElementById('crRoot');
    if (!S.root) return;
    clear(S.root).append(h('div', { class: 'cr-note' }, T('loading')));
    try { S.status = (await api('/status')); } catch (e) { return clear(S.root).append(h('div', { class: 'cr-note bad' }, e.message || T('e_generic'))); }
    render();
  }
  window.AresCreative = { show };
  if (document.body && document.body.dataset.page === 'creative') show();
})();
