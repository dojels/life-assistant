/* 聊天页的流式增强:执行反馈(步骤账本 / 流式思考 / 写入回执 + 撤销)。
 *
 * 三条纪律(照抄云朵项目踩过的坑,不重复踩):
 *   1. **手写 DOM + 按 id 复用节点** —— 整块 innerHTML='' 会吃掉用户的展开状态与滚动位置;
 *   2. **所有更新经 requestAnimationFrame 合并** —— 高频 token 天然合批,主线程不被淹;
 *   3. **思考只显示尾部一行** —— 旧实现把 5 万字塞进气泡并滚动跟随,实测阻塞主线程 31.6s。
 *     全文留在内存里,点一下才在弹窗里看。
 *
 * 禁 JS 时:聊天页发不出去(SSE 本来就需要 JS),但四个数据页仍是普通表单 POST。
 */
(function () {
  var form = document.getElementById('composer');
  if (!form || !window.fetch || !window.ReadableStream) return;
  var log = document.getElementById('log');
  var input = document.getElementById('text');
  var sendBtn = form.querySelector('button[type="submit"]') || form.querySelector('button');
  var stopBtn = document.getElementById('stop');

  // 撤销入口:五个域一个接口,前端不需要知道领域
  var UNDO_URL = '/api/receipts/undo';

  // ---------------------------------------------------------------- 工具函数
  function esc(text) {
    var d = document.createElement('div');
    d.textContent = String(text == null ? '' : text);
    return d.innerHTML;
  }

  function nowMs() { return Date.now(); }

  /** 耗时格式化 —— 与后端 la/agent/step_ledger.format_duration 同口径 */
  function fmtSecs(ms) {
    if (!ms || ms < 0) return '';
    var s = ms / 1000;
    if (s < 10) return s.toFixed(1) + 's';
    if (s < 60) return Math.round(s) + 's';
    var m = Math.floor(s / 60), r = Math.round(s % 60);
    if (m < 60) return m + 'm' + (r ? r + 's' : '');
    return Math.floor(m / 60) + 'h' + (m % 60) + 'm';
  }

  function kindIcon(kind, fallback) {
    if (kind === 'node') return '◆';
    if (kind === 'task') return '☑';
    if (kind === 'error') return '⚠';
    return fallback || '▸';
  }

  // ---------------------------------------------------------------- 步骤账本
  function TraceView(host) {
    this.host = host;
    this.rows = [];
    this.nodes = {};
    this.total = 0;
    this.dropped = 0;
    this.collapsed = false;
    this.startedAt = 0;
    this.meta = null;

    this.el = document.createElement('div');
    this.el.className = 'step-trace';
    this.el.hidden = true;
    this.head = document.createElement('div');
    this.head.className = 'trace-head';
    this.head.setAttribute('role', 'button');
    this.head.setAttribute('tabindex', '0');
    this.head.title = '点击展开/收起执行步骤';
    this.head.innerHTML = '<span class="trace-head-caret">▾</span>'
      + '<span class="trace-head-text"></span>'
      + '<span class="trace-head-hint">点击收起</span>';
    this.body = document.createElement('div');
    this.body.className = 'trace-body';
    this.el.appendChild(this.head);
    this.el.appendChild(this.body);
    host.appendChild(this.el);

    var self = this;
    function toggle(ev) {
      if (ev) ev.stopPropagation();
      self.setCollapsed(!self.collapsed);
    }
    this.head.addEventListener('click', toggle);
    this.head.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') toggle(ev);
    });
  }

  TraceView.prototype.rowEl = function (row) {
    var el = document.createElement('div');
    el.className = 'trace-row ' + (row.cls || 'k-tool') + (row.domain ? ' d-' + row.domain : '');
    el.dataset.stepId = row.id || '';
    el.innerHTML = '<span class="trace-row-state"></span>'
      + '<span class="trace-row-icon">' + esc(row.icon || kindIcon(row.kind)) + '</span>'
      + '<span class="trace-row-main"><span class="trace-row-title"></span>'
      + '<span class="trace-row-detail"></span></span>'
      + '<span class="trace-row-ms"></span>';
    return el;
  };

  TraceView.prototype.paint = function (el, row) {
    var status = row.status || 'running';
    var state = el.querySelector('.trace-row-state');
    state.className = 'trace-row-state st-' + status;
    if (status === 'running') {
      state.innerHTML = '<span class="thinking-dots"><span></span><span></span><span></span></span>';
    } else {
      state.textContent = status === 'failed' ? '⚠' : (status === 'pending' ? '○' : '✔');
    }
    el.querySelector('.trace-row-title').textContent = row.title || '';
    var detail = row.detail || '';
    var detailEl = el.querySelector('.trace-row-detail');
    detailEl.textContent = detail;
    detailEl.hidden = !detail;
    // 终态用服务端给的 duration_ms;进行中本地走秒(服务端不推心跳)
    var ms = (status === 'running')
      ? nowMs() - (Number(row.since) || this.startedAt || nowMs())
      : (row.duration_ms || 0);
    el.querySelector('.trace-row-ms').textContent = fmtSecs(ms);
    el.classList.toggle('is-running', status === 'running');
    el.classList.toggle('is-failed', status === 'failed');
    el.title = row.title || '';
  };

  TraceView.prototype.apply = function (payload) {
    if (!payload || typeof payload !== 'object') return;
    var row = payload.step;
    if (payload.total) this.total = payload.total;
    if (!row || !row.id) { this.renderHead(); return; }
    if (!this.startedAt) this.startedAt = Number(row.since) || nowMs();
    var el = this.nodes[row.id];
    if (el) {
      this.rows = this.rows.map(function (item) { return item.id === row.id ? row : item; });
      this.paint(el, row);
    } else {
      this.rows.push(row);
      el = this.rowEl(row);
      this.nodes[row.id] = el;
      this.paint(el, row);
      this.body.appendChild(el);
    }
    this.el.hidden = false;
    this.renderHead();
    if (payload.receipt_id) this.attachReceipt(row.id, payload.receipt_id);
    return el;
  };

  TraceView.prototype.sync = function (steps, meta) {
    if (Array.isArray(steps) && steps.length) {
      var self = this;
      var seen = {};
      var rows = [];
      steps.forEach(function (row) {
        if (!row || !row.id || seen[row.id]) return;
        seen[row.id] = true;
        rows.push(row);
        var el = self.nodes[row.id];
        if (!el || el.parentNode !== self.body) {
          el = self.rowEl(row);
          self.nodes[row.id] = el;
          self.body.appendChild(el);
        }
        self.paint(el, row);
      });
      Object.keys(this.nodes).forEach(function (id) {
        if (seen[id]) return;
        var node = self.nodes[id];
        if (node.parentNode) node.parentNode.removeChild(node);
        delete self.nodes[id];
      });
      this.rows = rows;
      this.el.hidden = false;
    }
    if (meta) {
      this.meta = meta;
      if (meta.total) this.total = meta.total;
      this.dropped = meta.dropped || 0;
    }
    this.renderHead();
  };

  /** 收口:把还在跑的行标成终态 —— 否则前端会留一个永远转圈的行 */
  TraceView.prototype.finish = function (status) {
    var self = this;
    var st = status || 'done';
    this.rows.forEach(function (row) {
      if (!row || row.status !== 'running') return;
      row.status = st;
      row.duration_ms = Math.max(0, nowMs() - (Number(row.since) || self.startedAt || nowMs()));
      var el = self.nodes[row.id];
      if (el) self.paint(el, row);
    });
    this.renderHead();
  };

  /** 每秒走秒(只重绘进行中的行,隐藏/折叠时零成本) */
  TraceView.prototype.tick = function () {
    if (this.el.hidden || this.collapsed) return;
    var self = this;
    this.rows.forEach(function (row) {
      if (!row || row.status !== 'running') return;
      var el = self.nodes[row.id];
      if (el) self.paint(el, row);
    });
    this.renderHead();
  };

  TraceView.prototype.setCollapsed = function (flag) {
    this.collapsed = !!flag;
    this.el.classList.toggle('collapsed', this.collapsed);
    this.head.setAttribute('aria-expanded', String(!this.collapsed));
    this.renderHead();
  };

  TraceView.prototype.elapsedText = function () {
    var ms;
    if (this.meta && this.meta.elapsed_ms) {
      ms = this.meta.elapsed_ms;
    } else {
      var first = this.rows[0] || {};
      var last = this.rows[this.rows.length - 1] || {};
      var from = Number(first.since) || this.startedAt;
      var to = (last.status === 'running' || !last.duration_ms)
        ? nowMs()
        : (Number(last.at) || (Number(last.since) || 0) + (last.duration_ms || 0));
      ms = to - from;
    }
    var text = fmtSecs(ms);
    return text ? ' · 用时 ' + text : '';
  };

  TraceView.prototype.label = function () {
    if (!this.rows.length) return '';
    return '已执行 ' + (this.total || this.rows.length) + ' 步' + this.elapsedText();
  };

  TraceView.prototype.renderHead = function () {
    if (!this.rows.length) { this.el.hidden = true; return; }
    this.el.hidden = false;
    var n = this.total || this.rows.length;
    var text = (this.collapsed ? '已执行 ' : '执行步骤 ') + n + ' 步';
    var bad = this.rows.filter(function (r) { return r && r.status === 'failed'; }).length;
    if (bad) text += ' · ' + bad + ' 步失败';
    if (this.dropped) text += ' · 较早 ' + this.dropped + ' 步已折叠';
    text += this.elapsedText();
    this.head.querySelector('.trace-head-text').textContent = text;
    this.head.querySelector('.trace-head-caret').textContent = this.collapsed ? '▸' : '▾';
    this.head.querySelector('.trace-head-hint').textContent =
      this.collapsed ? '点击展开' : '点击收起';
  };

  /** 把某一行的回执挂上去(回执卡自己带"撤销"按钮) */
  TraceView.prototype.attachReceipt = function (stepId, receiptId) {
    var el = this.nodes[stepId];
    if (!el) return;
    var title = el.querySelector('.trace-row-title');
    if (!title) return;
    var existing = title.querySelector('.step-receipt');
    if (existing && existing.dataset.receiptId === String(receiptId)) return;
    var chip = document.createElement('span');
    chip.className = 'step-receipt';
    chip.dataset.receiptId = String(receiptId);
    chip.textContent = '已写入';
    chip.title = '这一行改动了数据,见下面的回执卡(可撤销)';
    title.appendChild(chip);
  };

  // ---------------------------------------------------------------- 思考条
  var ThinkStrip = {
    make: function (host) {
      var box = document.createElement('div');
      box.className = 'thinking-strip';
      box.hidden = true;
      box.title = '点击查看完整思考过程';
      box.innerHTML = '<span class="thinking-strip-icon">💭</span>'
        + '<span class="thinking-strip-text"></span>'
        + '<span class="thinking-strip-hint">点击看全文</span>';
      host.appendChild(box);
      return box;
    },
    paint: function (box, text) {
      var line = box.querySelector('.thinking-strip-text');
      var tail = text.length > 90 ? '…' + text.slice(-90) : text;
      if (line.textContent !== tail) line.textContent = tail;
    },
    open: function (text) {
      var modal = document.getElementById('thinkFullModal');
      if (!modal) {
        modal = document.createElement('div');
        modal.id = 'thinkFullModal';
        modal.className = 'think-modal';
        modal.hidden = true;
        modal.innerHTML = '<div class="think-modal-box"><div class="think-modal-head">'
          + '<span>💭 完整思考过程</span>'
          + '<span class="think-modal-close" title="关闭">×</span></div>'
          + '<pre class="think-modal-body"></pre></div>';
        modal.addEventListener('click', function (ev) {
          if (ev.target === modal || ev.target.className === 'think-modal-close') {
            modal.hidden = true;
          }
        });
        document.body.appendChild(modal);
      }
      modal.querySelector('.think-modal-body').textContent = text || '(没有思考内容)';
      modal.hidden = false;
    }
  };

  // ---------------------------------------------------------------- 回执卡
  function receiptCard(item) {
    var card = document.createElement('div');
    card.className = 'receipt' + (item.undone ? ' undone' : '');
    // 两本回执账的 id 会撞号:撤销必须带来源("a:5" / "m:5")
    var receiptKey = (item.source ? item.source + ':' : '') + item.id;
    card.dataset.receiptId = String(receiptKey);
    var head = document.createElement('div');
    head.className = 'receipt-head';
    head.innerHTML = '<span class="receipt-dot">✎</span>'
      + '<span class="receipt-label"></span>'
      + '<span class="receipt-tag"></span>';
    head.querySelector('.receipt-label').textContent = item.label || '已写入';
    head.querySelector('.receipt-tag').textContent =
      (item.domain ? '[' + item.domain + '] ' : '') + '#' + item.id;
    card.appendChild(head);

    var detail = item.detail || {};
    var bits = [];
    if (detail.amount_text) bits.push(detail.amount_text);
    if (detail.category) bits.push('分类 ' + detail.category);
    if (detail.account) bits.push('账户 ' + detail.account);
    if (detail.ts) bits.push(String(detail.ts).replace('T', ' ').slice(0, 16));
    if (detail.local_date && !detail.ts) bits.push(detail.local_date);
    if (detail.value !== undefined && detail.unit) bits.push(detail.value + detail.unit);
    if (detail.status) bits.push('状态 ' + detail.status);
    if (bits.length) {
      var meta = document.createElement('div');
      meta.className = 'receipt-meta';
      meta.textContent = bits.join(' · ');
      card.appendChild(meta);
    }

    // ⭐ 产物(artifact)不是"领域写入":它要的是**下载**,不是撤销。
    //    产物是可再生的结果(清单/报表随时能重出),用户拿到它第一件想做的事是下载;
    //    而领域写入(记一笔账、录一道错题)才有"撤销"这个动作。
    if ((item.kind === 'artifact.file' || item.domain === 'artifact') && detail.artifact_id) {
      if (item.undone) {
        var gone = document.createElement('button');
        gone.type = 'button';
        gone.className = 'receipt-undo';
        gone.disabled = true;
        gone.textContent = '已删除';
        card.appendChild(gone);
      } else {
        // M13:生成的网页多一个「预览」—— 与 chat.html 里那条模板**同一条规则**,
        // 否则刷新前后会长得不一样(流式卡片是这里现拼的)。
        if (detail.preview_url) {
          var preview = document.createElement('a');
          preview.className = 'receipt-preview';
          preview.href = detail.preview_url;
          preview.target = '_blank';
          preview.rel = 'noopener';
          preview.textContent = '预览';
          preview.title = '在沙箱里打开这个网页';
          card.appendChild(preview);
        }
        var link = document.createElement('a');
        link.className = 'receipt-download';
        link.href = '/artifacts/' + encodeURIComponent(detail.artifact_id);
        link.textContent = '下载';
        link.title = '下载这份产物';
        card.appendChild(link);
      }
      return card;
    }

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'receipt-undo';
    if (item.undone) {
      btn.disabled = true;
      btn.textContent = '已撤销';
    } else if (!item.undoable) {
      btn.disabled = true;
      btn.textContent = '不可撤销';
      btn.title = '这类写入没有反向操作';
    } else {
      btn.textContent = '撤销这次写入';
      btn.addEventListener('click', function () { undo(receiptKey, card, btn); });
    }
    card.appendChild(btn);
    return card;
  }

  function undo(receiptId, card, btn) {
    btn.disabled = true;
    btn.textContent = '撤销中…';
    fetch(UNDO_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ receipt_id: receiptId })
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data && data.ok) {
        card.classList.add('undone');
        btn.textContent = '已撤销';
        var note = document.createElement('div');
        note.className = 'receipt-note';
        note.textContent = '↩ ' + (data.note || '已撤销');
        card.appendChild(note);
      } else {
        btn.disabled = false;
        btn.textContent = '撤销失败,重试';
        var warn = document.createElement('div');
        warn.className = 'receipt-note warn';
        warn.textContent = (data && data.error) || '撤销失败';
        card.appendChild(warn);
      }
      scrollDown();
    }).catch(function (err) {
      btn.disabled = false;
      btn.textContent = '撤销失败,重试';
      var warn = document.createElement('div');
      warn.className = 'receipt-note warn';
      warn.textContent = String(err);
      card.appendChild(warn);
    });
  }

  // ---------------------------------------------------------------- 提问卡
  // 统一反馈机制(M9):模型/领域可以问一句,用户点一下就答 —— 答案是**工具结果**,
  // 它接着往下做,不需要用户重新说一遍。
  // ⚠️ 一次提问只发一帧,卡片按 question_id 复用:重复帧不会画两张卡。
  function askCard(question) {
    var card = document.createElement('div');
    card.className = 'ask';
    card.dataset.questionId = question.id;

    var head = document.createElement('div');
    head.className = 'ask-head';
    head.innerHTML = '<span class="ask-mark">?</span><span class="ask-prompt"></span>';
    head.querySelector('.ask-prompt').textContent = question.prompt || '它想问你一件事';
    card.appendChild(head);

    var context = question.context || {};
    var chips = [];
    Object.keys(context).forEach(function (key) {
      var value = context[key];
      if (value === null || value === undefined || value === '') return;
      if (Object.prototype.toString.call(value) === '[object Array]') {
        if (!value.length) return;
        value = value.join(' / ');
      } else if (typeof value === 'object') {
        return;
      }
      chips.push(key + ' · ' + value);
    });
    if (chips.length) {
      var box = document.createElement('div');
      box.className = 'ask-context';
      chips.forEach(function (text) {
        var chip = document.createElement('span');
        chip.className = 'ask-chip';
        chip.textContent = text;
        box.appendChild(chip);
      });
      card.appendChild(box);
    }

    var form = document.createElement('form');
    form.className = 'ask-form';
    var options = document.createElement('div');
    options.className = 'ask-options';
    (question.options || []).forEach(function (option, index) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'small' + (index === 0 ? '' : ' neutral');
      btn.textContent = option.label;
      if (option.description) btn.title = option.description;
      btn.addEventListener('click', function () {
        // 显示用 label,回传给后端用 value —— 两者可能不同(create / 新建「宠物」)
        answerQuestion(question, card, [option.value || option.label], '', option.label);
      });
      options.appendChild(btn);
    });
    form.appendChild(options);

    if (question.allow_text !== false) {
      var row = document.createElement('div');
      row.className = 'ask-text';
      var input = document.createElement('input');
      input.type = 'text';
      input.placeholder = '或者自己写一句…';
      input.autocomplete = 'off';
      var submit = document.createElement('button');
      submit.type = 'button';
      submit.className = 'ghost small';
      submit.textContent = '就这样';
      submit.addEventListener('click', function () {
        var text = input.value.trim();
        if (!text) { input.focus(); return; }
        answerQuestion(question, card, [], text);
      });
      input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') { event.preventDefault(); submit.click(); }
      });
      row.appendChild(input);
      row.appendChild(submit);
      form.appendChild(row);
    }
    card.appendChild(form);
    return card;
  }

  function answerQuestion(question, card, selected, text, shown) {
    var buttons = card.querySelectorAll('button');
    Array.prototype.forEach.call(buttons, function (btn) { btn.disabled = true; });
    fetch('/api/questions/answer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question_id: question.id, selected: selected, text: text })
    }).then(function (response) { return response.json(); }).then(function (data) {
      var note = document.createElement('div');
      note.className = 'ask-answer';
      if (data && data.ok) {
        card.classList.add('answered');
        var form = card.querySelector('.ask-form');
        if (form) form.remove();
        note.innerHTML = '你的回答:<strong></strong>';
        note.querySelector('strong').textContent =
          shown || (data.selected && data.selected.length ? data.selected.join('、') : data.text)
          || '已收到';
        if (data.note) note.textContent += ' · ' + data.note;
        card.appendChild(note);
        // 让助手接着做:用户答完,那一轮还在等这个答案
        setStatus(state0, '已把你的回答交给助手,它继续…');
      } else {
        Array.prototype.forEach.call(buttons, function (btn) { btn.disabled = false; });
        note.className = 'ask-answer warn';
        note.textContent = (data && data.error) || '回答失败,再试一次';
        card.appendChild(note);
      }
      scrollDown();
    }).catch(function (err) {
      Array.prototype.forEach.call(buttons, function (btn) { btn.disabled = false; });
      var note = document.createElement('div');
      note.className = 'ask-answer warn';
      note.textContent = '回答失败:' + err;
      card.appendChild(note);
    });
  }

  // 当前这一轮的状态(回答问题时要把状态行也更新一下)。一次只有一轮在跑,所以是单例。
  var state0 = null;

  function addAsk(body, question) {
    if (!body || !question || !question.id) return;
    var existing = body.querySelector('.ask[data-question-id="' + question.id + '"]');
    if (existing) return;
    var holder = document.createElement('div');
    holder.className = 'asks';
    body.appendChild(holder);
    holder.appendChild(askCard(question));
    scrollDown();
    if (state0) setStatus(state0, '等你拍板…');
  }

  // ---------------------------------------------------------------- 气泡
  var ticker = null;

  // ⭐ D145:气泡上那句"谁在说话"是**现在她叫什么**(人格页可改)。
  //    名字由页面写在 <body data-assistant="..."> 上;没有(别的页)就还是「助手」。
  //    ⚠️ 不在这里写死人名:改名之后聊天页要跟着变,而脚本是全站共用的一个文件。
  var ASSISTANT = document.body.getAttribute('data-assistant') || '助手';

  function bubble(who, classes) {
    var node = document.getElementById('bubble').content.cloneNode(true);
    var msg = node.querySelector('.msg');
    if (who === '我') msg.classList.add('user'); else msg.classList.add('assistant');
    if (classes) msg.classList.add(classes);
    msg.querySelector('.who').textContent = who;
    log.appendChild(msg);
    var welcome = document.getElementById('welcome');
    if (welcome) welcome.hidden = true;   // 说话之后就收起欢迎态(空间让给对话)
    var root = log.lastElementChild;
    return root.querySelector('.body');
  }

  // 滚动容器是 #chat-body(手机上它吃掉整个视口),不是内联的 #log。
  // ⚠️ 不要在这里判"能不能滚就置 null":页面刚加载时它常常还没有溢出,
  //    一旦置成 null 就再也回不来了 —— 于是一整轮流式输出都不跟随(实测踩过)。
  var scroller = document.getElementById('chat-body');

  // 跟随状态机:只有"贴着底"才自动跟随;用户一旦上滚,立刻停止抢滚动条。
  // 以前每次输出都无条件跳到底(阈值 120px 过宽),读旧消息时会被反复拽回去。
  var STICK_GAP = 48;
  var stick = true;

  function gapToBottom() {
    if (scroller) {
      return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
    }
    var doc = document.documentElement;
    return doc.scrollHeight - window.scrollY - window.innerHeight;
  }

  function jumpToBottom() {
    if (scroller) { scroller.scrollTop = scroller.scrollHeight; return; }
    window.scrollTo(0, document.documentElement.scrollHeight);
  }

  /** follow():本轮输出要不要自动贴底;force():无视状态直接贴底(用户自己发消息时用) */
  function scrollDown(force) {
    if (force) { stick = true; jumpToBottom(); return; }
    if (!stick) return;
    jumpToBottom();
  }

  // 用户手动滚动:离开底部就解除跟随,回到附近就恢复
  var scrollHost = scroller || window;
  scrollHost.addEventListener('scroll', function () {
    stick = gapToBottom() <= STICK_GAP;
  }, { passive: true });

  // 发消息前输入框长高、图片加载完 —— 这些都会改变高度,贴底状态要跟着重算
  window.addEventListener('load', function () { scrollDown(true); });

  // 唯一的 1s 节拍器:驱动状态行"已等待 Ns"与步骤行走秒(不新增定时器)
  function ensureTicker(view, state) {
    if (ticker) return;
    ticker = setInterval(function () {
      if (state.done) { clearInterval(ticker); ticker = null; return; }
      if (view) view.tick();
      if (state.statusEl && !state.statusEl.hidden && state.statusSince) {
        var secs = Math.floor((nowMs() - state.statusSince) / 1000);
        var el = state.statusEl.querySelector('.flow-status-elapsed');
        if (el) el.textContent = secs >= 3 ? ' · 已等待 ' + secs + 's' : '';
      }
    }, 1000);
  }

  function setStatus(state, msg) {
    if (!state.statusEl) return;
    if (msg === state.statusText) return;
    state.statusText = msg;
    state.statusSince = nowMs();
    state.statusEl.hidden = !msg;
    state.statusEl.innerHTML = msg
      ? '<span class="thinking-dots"><span></span><span></span><span></span></span> '
        + esc(msg) + '<span class="flow-status-elapsed"></span>'
      : '';
  }

  function addReceipt(body, item) {
    if (!body || !item || !item.id) return;
    var host = body.querySelector('.receipts');
    if (!host) return;
    var key = (item.source ? item.source + ':' : '') + item.id;
    var existing = host.querySelector('.receipt[data-receipt-id="' + key + '"]');
    if (existing) return;
    host.hidden = false;
    host.appendChild(receiptCard(item));
    scrollDown();
  }

  // ---------------------------------------------------------------- 一轮对话
  function run(text, body) {
    var state = { done: false, statusText: '', statusSince: 0, thinking: '' };
    state0 = state;   // 提问卡回答之后要更新状态行,靠它

    body.innerHTML = '';
    var statusEl = document.createElement('div');
    statusEl.className = 'flow-status';
    statusEl.hidden = true;
    body.appendChild(statusEl);
    state.statusEl = statusEl;

    var view = new TraceView(body);
    var thinkBox = ThinkStrip.make(body);

    var answer = document.createElement('div');
    answer.className = 'answer';
    body.appendChild(answer);

    var receipts = document.createElement('div');
    receipts.className = 'receipts';
    receipts.hidden = true;
    body.appendChild(receipts);

    ensureTicker(view, state);

    var controller = new AbortController();
    stopBtn.hidden = false;
    stopBtn.onclick = function () {
      controller.abort();
      setStatus(state, '已停止');
      stopBtn.hidden = true;
    };

    // 思考增量:只显示尾部一行,150ms 节流(不读 scrollHeight、不滚动)
    var thinkPaintAt = 0;
    function pushThinking(piece) {
      state.thinking += piece;
      if (state.thinking.length > 50000) state.thinking = state.thinking.slice(-50000);
      thinkBox.hidden = false;
      var now = nowMs();
      if (now - thinkPaintAt < 150) return;
      thinkPaintAt = now;
      ThinkStrip.paint(thinkBox, state.thinking);
      scrollDown();
    }

    fetch('/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ text: text }),
      signal: controller.signal
    }).then(function (response) {
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      var eventName = 'message';
      var dataLines = [];

      function dispatch() {
        var payload = dataLines.join('\n');
        eventName = eventName || 'message';
        dataLines = [];
        var name = eventName;
        eventName = 'message';
        if (!payload) return;
        var data;
        try { data = JSON.parse(payload); } catch (e) { data = payload; }

        if (name === 'status') {
          setStatus(state, (data && data.msg) || '');
        } else if (name === 'step' || name === 'step_update') {
          view.apply(data);
          scrollDown();
        } else if (name === 'thinking') {
          pushThinking(typeof data === 'string' ? data : String(data));
        } else if (name === 'thinking_end') {
          ThinkStrip.paint(thinkBox, state.thinking);
          thinkBox.classList.add('think-done');
        } else if (name === 'html') {
          setStatus(state, '');
          answer.innerHTML = typeof data === 'string' ? data : '';
          scrollDown();
        } else if (name === 'receipt') {
          addReceipt(body, data && data.receipt);
        } else if (name === 'ask') {
          // 统一反馈机制:它在等你拍板。答完这一轮会接着往下跑。
          addAsk(body, data);
        } else if (name === 'ask_done') {
          var node = body.querySelector('.ask[data-question-id="' + (data && data.id) + '"]');
          if (node) node.classList.add('answered');
        } else if (name === 'notice') {
          var note = document.createElement('div');
          note.className = 'warn';
          note.textContent = typeof data === 'string' ? data : JSON.stringify(data);
          body.appendChild(note);
        } else if (name === 'error') {
          var err = document.createElement('div');
          err.className = 'warn';
          err.textContent = typeof data === 'string' ? data : JSON.stringify(data);
          body.appendChild(err);
          view.finish('failed');
        } else if (name === 'done') {
          if (data && Array.isArray(data.steps) && data.steps.length) {
            view.sync(data.steps, data.steps_meta);
          }
          view.finish('done');
          view.meta = (data && data.steps_meta) || view.meta;
          view.renderHead();
          // 有正文、且没有失败步,才自动收起(别把失败原因挡住)
          if (answer.textContent.trim() && !view.rows.some(function (r) { return r.status === 'failed'; })) {
            view.setCollapsed(true);
          }
          // 回执兜底:即使 step 帧丢了,这里也能把这一轮的写入回执铺出来
          if (data && Array.isArray(data.receipts)) {
            data.receipts.forEach(function (item) { addReceipt(body, item); });
          }
        }
      }

      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) {
            if (dataLines.length) dispatch();
            state.done = true;
            view.finish('done');
            setStatus(state, '');
            stopBtn.hidden = true;
            return;
          }
          buffer += decoder.decode(chunk.value, { stream: true });
          var parts = buffer.split('\n');
          buffer = parts.pop();
          parts.forEach(function (line) {
            if (line.indexOf('event:') === 0) {
              eventName = line.slice(6).trim();
            } else if (line.indexOf('data:') === 0) {
              dataLines.push(line.slice(5).replace(/^ /, ''));
            } else if (line === '') {
              dispatch();
            }
          });
          scrollDown();
          return pump();
        });
      }
      return pump();
    }).catch(function (err) {
      if (err && err.name === 'AbortError') {
        view.finish('done');
      } else {
        var warn = document.createElement('div');
        warn.className = 'warn';
        warn.textContent = '出错了:' + err;
        body.appendChild(warn);
        view.finish('failed');
      }
      state.done = true;
      setStatus(state, '');
      stopBtn.hidden = true;
    });
  }

  // ---------------------------------------------------------------- 附件(拍照录错题)
  // 与微信那条链路**完全同源**:POST /chat/attach -> 素材层 -> 视觉识别 -> 直接录入。
  // 差别只在入口:这里多一个回形针,桌面上还支持拖拽与粘贴。
  var attachBtn = document.getElementById('attach');
  var attachInput = document.getElementById('attach-file');

  function attachBubble(files) {
    var me = bubble('我');
    var box = document.createElement('div');
    box.className = 'attach-preview';
    Array.prototype.forEach.call(files, function (file) {
      if ((file.type || '').indexOf('image/') === 0) {
        var img = document.createElement('img');
        img.className = 'shot-sm';
        img.src = URL.createObjectURL(file);
        img.onload = function () { URL.revokeObjectURL(img.src); };
        box.appendChild(img);
      } else {
        var chip = document.createElement('span');
        chip.className = 'pill plain';
        chip.textContent = '📎 ' + file.name;
        box.appendChild(chip);
      }
    });
    me.appendChild(box);
    return bubble(ASSISTANT);
  }

  function uploadFiles(files) {
    if (!files || !files.length) return;
    var body = attachBubble(files);
    var status = document.createElement('div');
    status.className = 'muted';
    status.textContent = '正在识别…(视觉模型,通常十几秒)';
    body.appendChild(status);

    var data = new FormData();
    Array.prototype.forEach.call(files, function (file) { data.append('files', file); });
    fetch('/chat/attach', { method: 'POST', body: data })
      .then(function (response) { return response.json(); })
      .then(function (result) {
        status.remove();
        var note = document.createElement('div');
        note.className = result && result.ok ? 'attach-note' : 'warn';
        note.textContent = (result && result.message) || '上传失败';
        body.appendChild(note);
        if (result && result.recorded && result.recorded.length) {
          var links = document.createElement('div');
          links.className = 'attach-links';
          result.recorded.forEach(function (item) {
            var a = document.createElement('a');
            a.className = 'pill tinted';
            a.href = '/study/problem/' + item.problem_id;
            a.textContent = '错题 #' + item.problem_id + ' · ' + (item.subject || '');
            links.appendChild(a);
          });
          body.appendChild(links);
        }
        if (result && result.receipt) addReceipt(body, result.receipt);
        scrollDown();
      })
      .catch(function (err) {
        status.remove();
        var warn = document.createElement('div');
        warn.className = 'warn';
        warn.textContent = '上传出错了:' + err;
        body.appendChild(warn);
      });
  }

  if (attachBtn && attachInput) {
    attachBtn.addEventListener('click', function () { attachInput.click(); });
    attachInput.addEventListener('change', function () {
      uploadFiles(attachInput.files);
      attachInput.value = '';
    });
    // 拖拽与粘贴:桌面端顺手,手机端用不到(不影响)
    ['dragover', 'drop'].forEach(function (name) {
      document.addEventListener(name, function (event) { event.preventDefault(); });
    });
    document.addEventListener('drop', function (event) {
      if (event.dataTransfer && event.dataTransfer.files) uploadFiles(event.dataTransfer.files);
    });
    document.addEventListener('paste', function (event) {
      var items = (event.clipboardData && event.clipboardData.files) || [];
      if (items.length) { event.preventDefault(); uploadFiles(items); }
    });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var text = input.value.trim();
    if (!text) return;
    input.value = '';
    if (sendBtn) sendBtn.disabled = true;
    bubble('我').textContent = text;
    var body = bubble(ASSISTANT);
    run(text, body);
    setTimeout(function () { if (sendBtn) sendBtn.disabled = false; }, 400);
  });

  // 页面底部已有历史回执:把"撤销最近一次"这件事也放到聊天页上
  var tray = document.getElementById('receipt-tray');
  if (tray) {
    Array.prototype.forEach.call(tray.querySelectorAll('[data-receipt-id]'), function (node) {
      var btn = node.querySelector('.receipt-undo');
      if (!btn || btn.disabled) return;
      btn.addEventListener('click', function () {
        undo(String(node.dataset.receiptId), node, btn);
      });
    });
  }

  // 建议胶囊:只把话填进输入框,不替用户发出去 —— 写入是直接执行的,发之前该让他看一眼
  Array.prototype.forEach.call(
    document.querySelectorAll('[data-say]'),
    function (chip) {
      chip.addEventListener('click', function () {
        input.value = chip.dataset.say || '';
        input.focus();
        scrollDown(true);
      });
    }
  );

  // 首屏:直接贴底(不等 load,长历史本来就该停在最新一条)
  scrollDown(true);
  // 输入法/键盘弹出会改视口高度,贴底状态跟着重算一次
  window.addEventListener('resize', function () { if (stick) jumpToBottom(); });
})();

/* 日历页「复制订阅链接」:独立 IIFE,不能放进上面那段 ——
   那段在没有输入框的页面上会直接 return(日历页本来就没有 composer)。 */
(function () {
  function fallbackCopy(text) {
    var area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', 'readonly');
    area.style.position = 'fixed';
    area.style.left = '-9999px';
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(area);
    return ok;
  }

  document.addEventListener('click', function (event) {
    var target = event.target;
    var button = target && target.closest ? target.closest('.js-copy') : null;
    if (!button) return;
    event.preventDefault();
    var text = button.getAttribute('data-copy') || '';
    if (!text) return;
    var done = function () {
      var label = button.textContent;
      button.textContent = '已复制';
      window.setTimeout(function () { button.textContent = label; }, 1600);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {
        if (fallbackCopy(text)) done();
      });
    } else if (fallbackCopy(text)) {
      done();
    }
  });
})();
