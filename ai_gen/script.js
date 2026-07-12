const chats = {
    smooth: [
        { sender: 'A', text: 'Do you build any model kits? I saw some on your shelf in your profile pic.' },
        { sender: 'B', text: 'Yeah! I mostly build Gundam kits. I really prefer the bulky, industrial-looking ones.' },
        { sender: 'A', text: 'Oh nice, those always look like they take a ton of work.' },
        { sender: 'B', text: 'They do, the part counts are insane. I just finished the MG Sazabi Ver. Ka last week. Took me a month. Do you build at all, or just looking?' },
        { sender: 'A', text: 'I used to build airplanes when I was a kid, but nothing that complex.' },
        { sender: 'B', text: 'Airplanes are cool! Especially the WWII era ones with crazy details.' },
        { sender: 'A', text: 'Exactly, fixing the decals was always the hardest part though.' },
        { sender: 'B', text: 'Tell me about it... Waterslide decals are my literal nightmare. I rip them half the time.' },
        { sender: 'A', text: 'You gotta use mark softer! It makes them melt right onto the plastic curves.' },
        { sender: 'B', text: 'Really? I need to look into that. Would save me hours of frustration.' },
        { sender: 'A', text: 'Yeah I can send you a link to the brand I use if you want.' },
        { sender: 'B', text: 'That would be awesome, thanks man!' }
    ],
    struggling: [
        { sender: 'A', text: 'Man, I am so stressed out about the graduate school entrance process right now.' },
        { sender: 'B', text: 'Damn.' },
        { sender: 'A', text: 'Yeah, it feels like my portfolio just isn\'t going to be enough compared to everyone else\'s. I\'ve been staring at my final project code all day.' },
        { sender: 'B', text: 'That sucks.' },
        { sender: 'A', text: 'I just keep finding bugs in my Python script and it\'s driving me crazy. Like why won\'t it just run once without throwing an exception?' },
        { sender: 'B', text: 'RIP.' },
        { sender: 'A', text: 'And the deadline is literally next Friday. I haven\'t even started my statement of purpose.' },
        { sender: 'B', text: 'Oof.' },
        { sender: 'A', text: 'I genuinely don\'t know if I can finish all of this in time. Might just have to delay my application a year.' },
        { sender: 'B', text: 'Yeah maybe.' },
        { sender: 'A', text: 'I don\'t know, it just feels like I\'m drowning in work and none of it is good enough.' },
        { sender: 'B', text: 'Sorry to hear that.' }
    ],
    neutral: [
        { sender: 'A', text: 'Hey!' },
        { sender: 'B', text: 'Hey, what\'s up?' },
        { sender: 'A', text: 'Not much, just taking a break from studying. Saw you\'re in the Information Management department too?' },
        { sender: 'B', text: 'Yeah, currently dying in my system architecture class lol.' },
        { sender: 'A', text: 'Ah, I had that last semester. The midterm for that was brutal.' },
        { sender: 'B', text: 'Don\'t remind me, it\'s coming up next week.' },
        { sender: 'A', text: 'Have you tried using any AI coding assistants for your projects yet?' },
        { sender: 'B', text: 'I looked into a few, but I haven\'t actually set anything up. Are they worth it?' },
        { sender: 'A', text: 'Honestly, yeah. It saves a ton of time on all the basic boilerplate code.' },
        { sender: 'B', text: 'That sounds nice. I\'m building a personal project right now using a physics engine and the setup is taking forever.' },
        { sender: 'A', text: 'Yeah setting up environments is always the worst part of any project.' },
        { sender: 'B', text: 'True. Once you start actually writing the logic it\'s not so bad.' }
    ]
};

let currentScenario = 'smooth';
let currentMessageIndex = 0;
let lastAgentRunIndex = 0;
let activeAiRole = 'friend'; 
let manualAiTriggers = 0;
let lastMessageTime = Date.now();

const chatContainer = document.getElementById('chat-messages');
const btnProgress = document.getElementById('btn-progress');
const btnReset = document.getElementById('btn-reset');
const selectScenario = document.getElementById('chat-selector');
const msgCountEl = document.getElementById('msg-count');
const totalMsgsEl = document.getElementById('total-msgs');
const aiRoleEl = document.getElementById('ai-role');
const suggestionZone = document.getElementById('ai-suggestion-zone');
const inputField = document.querySelector('.input-wrapper input[type="text"]');
const btnAiHelp = document.getElementById('btn-ai-help');

function renderMessage(msg) {
    const div = document.createElement('div');
    div.classList.add('message');
    div.classList.add(msg.sender === 'A' ? 'msg-a' : 'msg-b');
    div.innerText = msg.text;
    chatContainer.appendChild(div);
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

function updateUIInfo(scenario) {
    const msgs = chats[scenario];
    totalMsgsEl.innerText = msgs.length;
    msgCountEl.innerText = currentMessageIndex;
    aiRoleEl.innerText = activeAiRole.toUpperCase();

    // Update suggestion appearance classes based on role
    suggestionZone.className = 'ai-suggestion-zone'; // reset
    suggestionZone.classList.add(`ai-role-${activeAiRole}`);
    
    // Clear out suggestion text from previous interactions
    suggestionZone.innerHTML = '';
    
    // Reset button visibility states
    btnAiHelp.classList.remove('visibility-high', 'visibility-medium', 'visibility-low');
    
    if (activeAiRole === 'friend' || activeAiRole === 'adviser') {
        btnAiHelp.classList.add('visibility-high');
        inputField.placeholder = "Type a message...";
    } else if (activeAiRole === 'facilitator') {
        btnAiHelp.classList.add('visibility-medium');
        inputField.placeholder = "Type a message...";
    } else if (activeAiRole === 'mentor') {
        btnAiHelp.classList.add('visibility-low');
        inputField.placeholder = "Type a message... (AI is observing, click input to ask)";
    }
}

function progressChat() {
    const msgs = chats[currentScenario];
    if (currentMessageIndex < msgs.length) {
        const msg = msgs[currentMessageIndex];
        if (!msg.timestamp) {
            msg.timestamp = new Date().toISOString();
        }
        renderMessage(msg);
        trackMessage(msg);
        lastMessageTime = Date.now();
        currentMessageIndex++;
        updateUIInfo(currentScenario);

        if (currentMessageIndex > 0 && currentMessageIndex % 5 === 0) {
            triggerAgent1();
        }
    }
}

async function trackMessage(msg) {
    try {
        await fetch('http://localhost:3000/api/track-message', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                sessionId: `chat_${currentScenario}`,
                message: msg
            })
        });
    } catch (e) {
        console.error("Failed to track message:", e);
    }
}

async function triggerAgent1() {
    console.log(`Triggering Agent 1... sending chat log`);
    const msgs = chats[currentScenario].slice(0, currentMessageIndex);
    try {
        const response = await fetch('http://localhost:3000/api/semantic-plan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                sessionId: `chat_${currentScenario}`,
                messages: msgs,
                manual_ai_triggers: manualAiTriggers
            })
        });
        const data = await response.json();
        console.log("Semantic Plan received:", data);
        
        if (data.current_role) {
            activeAiRole = data.current_role.toLowerCase();
            updateUIInfo(currentScenario);
        }
    } catch (e) {
        console.error("Agent 1 trigger failed:", e);
    }
}

function resetChat() {
    currentMessageIndex = 0;
    lastAgentRunIndex = 0;
    activeAiRole = 'friend';
    chatContainer.innerHTML = '';
    updateUIInfo(currentScenario);
}

// Event Listeners
btnProgress.addEventListener('click', progressChat);
btnReset.addEventListener('click', resetChat);
selectScenario.addEventListener('change', (e) => {
    currentScenario = e.target.value;
    resetChat();
});

async function callGenerateSuggestion(forceAssist = false) {
    if (activeAiRole === 'mentor') {
        inputField.placeholder = "Generating suggestion...";
    } else {
        suggestionZone.innerHTML = '<p><em>Generating suggestion...</em></p>';
    }
    
    if (!forceAssist) {
        manualAiTriggers++;
    }
    
    const t_invoke = (Date.now() - lastMessageTime) / 1000.0;
    const inputText = inputField.value;
    const msgs = chats[currentScenario].slice(0, currentMessageIndex);
    
    try {
        const response = await fetch('http://localhost:3000/api/generate-suggestion', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                sessionId: `chat_${currentScenario}`,
                messages: msgs,
                t_invoke: t_invoke,
                input_text: inputText,
                force_assist: forceAssist
            })
        });
        const data = await response.json();
        console.log("Agent 2 API Response:", data);
        
        if (data.ui_nudge) {
            let auditHtml = '';
            if (data.audit_trail) {
                auditHtml = `<div class="audit-trail" style="font-size: 0.85em; color: #a3a3a3; margin-top: 8px; padding-top: 8px; border-top: 1px solid rgba(255,255,255,0.1);">
                    <strong>Audit Trail:</strong> ${data.audit_trail}
                </div>`;
            }

            if (activeAiRole === 'mentor' && !forceAssist) {
                suggestionZone.innerHTML = `
                    <div class="mentor-tooltip">
                        <p>${data.ui_nudge}</p>
                        ${auditHtml}
                        <div style="margin-top: 8px;">
                            <button id="btn-force-assist" class="force-assist-btn">Force Assist</button>
                        </div>
                    </div>
                `;
                inputField.placeholder = "Type a message...";
                
                const btnForceAssist = document.getElementById('btn-force-assist');
                let confirmCount = 0;
                btnForceAssist.addEventListener('click', () => {
                    if (confirmCount === 0) {
                        btnForceAssist.innerText = "Are you sure? Click to override.";
                        btnForceAssist.classList.add("force-assist-confirm");
                        confirmCount++;
                    } else {
                        btnForceAssist.innerText = "Generating...";
                        callGenerateSuggestion(true);
                    }
                });
            } else {
                inputField.placeholder = "Type a message...";
                suggestionZone.innerHTML = `<p>${data.ui_nudge}</p>${auditHtml}`;
            }
        } else if (data.error) {
            suggestionZone.innerHTML = `<p style="color:red;">Error: ${data.error}</p>`;
        }
    } catch (e) {
        console.error("Agent 2 trigger failed:", e);
        if (activeAiRole === 'mentor') {
            inputField.placeholder = "Failed to get suggestion.";
        } else {
            suggestionZone.innerHTML = `<p style="color:red;">Failed to get suggestion.</p>`;
        }
    }
}

btnAiHelp.addEventListener('click', () => {
    callGenerateSuggestion(false);
});

// Init
updateUIInfo(currentScenario);

inputField.addEventListener('click', () => {
    if (activeAiRole === 'mentor') {
        btnAiHelp.click();
    }
});
