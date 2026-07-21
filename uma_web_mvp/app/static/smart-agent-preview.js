const messagesEl = document.getElementById("messages");
    const inputEl = document.getElementById("input");
    const composer = document.getElementById("composer");
    const historyBtn = document.getElementById("historyBtn");
    const historyPanel = document.getElementById("historyPanel");

const avatarSrc = "/assets/branding/favicon-32x32.png?v=mizuhara";

    function scrollBottom() {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }

function agentAvatar() {
  return `
    <div class="avatar">
      <img src="${avatarSrc}" alt="" />
    </div>
  `;
}

function bindAvatarFallback(root) {
  root.querySelectorAll(".avatar img").forEach((image) => {
    image.addEventListener("error", () => {
      const parent = image.parentElement;
      image.remove();
      if (parent) {
        parent.textContent = "击";
      }
    }, { once: true });
  });
}

function addAgent(text, type = "normal") {
  const msg = document.createElement("div");
  msg.className = "msg agent";
      const bubbleClass = type === "event" ? "bubble event" : "bubble";
      msg.innerHTML = `
        ${agentAvatar()}
        <div class="agent-stack">
          <div class="agent-name">小击击</div>
          <div class="${bubbleClass}"></div>
        </div>
  `;
  bindAvatarFallback(msg);
  msg.querySelector(".bubble").textContent = text;
  messagesEl.appendChild(msg);
  scrollBottom();
    }

    function addUser(text) {
      const msg = document.createElement("div");
      msg.className = "msg user";
      msg.innerHTML = `<div class="bubble"></div>`;
      msg.querySelector(".bubble").textContent = text;
      messagesEl.appendChild(msg);
      scrollBottom();
    }

    function addImageCard(jobCode) {
      const msg = document.createElement("div");
      msg.className = "msg agent";
      msg.innerHTML = `
        ${agentAvatar()}
        <div class="agent-stack">
          <div class="agent-name">小击击</div>
          <div class="bubble image-card">
            <div class="image-ph">生成图预览</div>
            <div class="job-code">已为你生成图片 · ${jobCode}</div>
      </div>
    </div>
  `;
  bindAvatarFallback(msg);
  messagesEl.appendChild(msg);
  scrollBottom();
}

    function sleep(ms) {
      return new Promise(resolve => setTimeout(resolve, ms));
    }

    async function fakeGenerateFlow() {
      const steps = [
        "正在理解你的需求……",
        "正在查找匹配的角色 Tag……",
        "正在匹配合适的提示词片段……",
        "正在选择合适的图片工作流……",
        "正在寻找合适的 LoRA……",
        "正在选择合适画幅……",
        "正在整理最终提示词……",
        "正在提交生图任务……",
        "任务已加入队列：GEN-PREVIEW123"
      ];

      for (const step of steps) {
        await sleep(650);
        addAgent(step, "event");
      }

      await sleep(900);
      addImageCard("GEN-PREVIEW123");
    }

    function autoGrow() {
      inputEl.style.height = "auto";
      inputEl.style.height = Math.min(inputEl.scrollHeight, 150) + "px";
    }

    inputEl.addEventListener("input", autoGrow);

    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        composer.requestSubmit();
      }
    });

    composer.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = inputEl.value.trim();
      if (!text) return;

      addUser(text);
      inputEl.value = "";
      autoGrow();

      if (/生成|出图|开始|就这样|可以/.test(text)) {
        addAgent("收到，击击。这个需求已经可以开始生成了，我会先检查角色、提示词、工作流和画幅。");
        await fakeGenerateFlow();
      } else {
        await sleep(500);
        addAgent("收到，击击。我先记住这个方向。你可以继续补充角色、场景、画风、构图，或者直接说“生成吧”。");
      }
    });

    document.getElementById("backBtn").addEventListener("click", () => {
      window.location.href = "/";
    });

    document.getElementById("newChatBtn").addEventListener("click", () => {
      messagesEl.innerHTML = "";
      addAgent("新聊天已开启。击击，告诉我这次想生成什么图。");
    });

    document.getElementById("clearBtn").addEventListener("click", () => {
      messagesEl.innerHTML = "";
      addAgent("当前聊天记忆已清空。击击，我会重新理解你的需求。");
    });

    historyBtn.addEventListener("click", () => {
      historyPanel.classList.toggle("open");
    });

    document.addEventListener("click", (e) => {
      if (!historyPanel.contains(e.target) && e.target !== historyBtn) {
        historyPanel.classList.remove("open");
      }
    });

    addAgent("击击，告诉我你想生成什么图。我可以帮你匹配角色 Tag、提示词风格、工作流、LoRA 和画幅。");

