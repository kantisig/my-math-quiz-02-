import os
import json
import time
import random
from pathlib import Path
from typing import Dict, List, Any, Optional
from fastapi import FastAPI, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import io

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="MathQuiz Pro")

class LiveQuizState:
    def __init__(self):
        self.raw_quiz: Optional[Dict[str, Any]] = None
        self.is_live: bool = False
        self.live_duration_seconds: int = 0
        self.start_timestamp: float = 0.0
        self.shuffled_questions: List[Dict[str, Any]] = []
        self.submissions: List[Dict[str, Any]] = []
        self.teachers: List[WebSocket] = []
        self.students: List[WebSocket] = []

    def set_quiz(self, quiz_data: Dict[str, Any]):
        self.raw_quiz = quiz_data
        self.is_live = False
        self.submissions = []

    def start_live(self, duration_minutes: int):
        if not self.raw_quiz: return False
        self.is_live = True
        self.live_duration_seconds = duration_minutes * 60
        self.start_timestamp = time.time()
        self.submissions = []
        
        # Shuffle Questions & Choices
        qs = list(self.raw_quiz["questions"])
        random.shuffle(qs)
        for q in qs:
            # เก็บเฉลยเดิมไว้เทียบก่อนสุ่ม
            correct_choice = q["choices"][q["correct_answer_index"]]
            random.shuffle(q["choices"])
            q["correct_answer_index"] = q["choices"].index(correct_choice)
        
        self.shuffled_questions = qs
        return True

    def get_remaining(self):
        if not self.is_live: return 0
        rem = int(self.live_duration_seconds - (time.time() - self.start_timestamp))
        if rem <= 0: self.is_live = False; return 0
        return rem

    def add_submission(self, name, answers):
        if not self.raw_quiz: return None
        score = 0
        details = {}
        for q in self.shuffled_questions:
            qid = str(q["id"])
            user_ans = answers.get(qid)
            correct = q["correct_answer_index"]
            is_correct = (user_ans == correct)
            if is_correct: score += 1
            details[qid] = {
                "chosen": user_ans,
                "correct": correct,
                "is_correct": is_correct,
                "explanation": q.get("explanation", "")
            }
        
        entry = {
            "name": name,
            "score": score,
            "total": len(self.shuffled_questions),
            "submitted_at": time.strftime("%H:%M:%S"),
            "details": details
        }
        self.submissions.append(entry)
        return entry

state = LiveQuizState()

# --- Routes ---
@app.get("/", response_class=HTMLResponse)
async def student_page(request: Request):
    return templates.TemplateResponse("student.html", {"request": request})

@app.get("/teacher", response_class=HTMLResponse)
async def teacher_page(request: Request):
    return templates.TemplateResponse("teacher.html", {"request": request})

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    data = json.loads((await file.read()).decode())
    state.set_quiz(data)
    return {"status": "success"}

@app.post("/api/start-timer")
async def start(request: Request):
    d = await request.json()
    if state.start_live(int(d.get("duration", 10))):
        await broadcast(state.students, {"type": "START", "seconds": state.live_duration_seconds})
        await broadcast(state.teachers, {"type": "STATUS_UPDATE", "is_live": True})
        return {"status": "success"}
    return {"status": "error"}

@app.get("/api/get-quiz")
async def get_quiz():
    if not state.is_live: return JSONResponse({"error": "Exam not started"}, 400)
    # Hide correct answers
    safe_qs = [{"id": q["id"], "question": q["question"], "choices": q["choices"]} for q in state.shuffled_questions]
    return {"title": state.raw_quiz["quiz_title"], "questions": safe_qs}

@app.post("/api/submit")
async def submit(request: Request):
    d = await request.json()
    res = state.add_submission(d["student_name"], d["answers"])
    await broadcast(state.teachers, {"type": "NEW_SUBMISSION", "analytics": get_analytics()})
    return res

@app.get("/api/export-csv")
async def export_csv():
    output = io.StringIO()
    output.write("Student Name,Score,Total,Time\n")
    for s in state.submissions:
        output.write(f"{s['name']},{s['score']},{s['total']},{s['submitted_at']}\n")
    return StreamingResponse(io.BytesIO(output.getvalue().encode()), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=results.csv"})

def get_analytics():
    if not state.raw_quiz: return {}
    subs = state.submissions
    total_q = len(state.shuffled_questions)
    # Simple analytics
    scores = [s["score"] for s in subs]
    hist = [scores.count(i) for i in range(total_q + 1)]
    
    # Item analysis (wrong count)
    wrong_counts = []
    for q in state.shuffled_questions:
        qid = str(q["id"])
        wrong = sum(1 for s in subs if not s["details"].get(qid, {}).get("is_correct", False))
        wrong_counts.append({"id": qid, "question": q["question"], "wrong": wrong})
    
    return {
        "total_students": len(subs),
        "histogram": hist,
        "submissions": subs[::-1], # Latest first
        "wrong_ranking": sorted(wrong_counts, key=lambda x: x["wrong"], reverse=True)[:5]
    }

async def broadcast(client_list, message):
    for ws in client_list[:]:
        try: await ws.send_json(message)
        except: client_list.remove(ws)

@app.websocket("/ws/{role}")
async def websocket_endpoint(websocket: WebSocket, role: str):
    await websocket.accept()
    target_list = state.teachers if role == "teacher" else state.students
    target_list.append(websocket)
    try:
        if role == "teacher":
            await websocket.send_json({"type": "INIT", "is_live": state.is_live, "analytics": get_analytics()})
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        target_list.remove(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)