
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import List, Optional
import psycopg2
from psycopg2.extras import RealDictCursor
import jwt
from datetime import datetime, timedelta
import hashlib
import os
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
DATABASE_URL = ""
SECRET_KEY = ""
ADMIN_API_KEY = ""

# Database connection
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database connection failed: {str(e)}")

# Models
class UserCreate(BaseModel):
    username: str
    password: str
    email: str
    is_admin: bool = False

class UserLogin(BaseModel):
    username: str
    password: str

class Train(BaseModel):
    train_number: str
    source: str
    destination: str
    total_seats: int

class BookingCreate(BaseModel):
    train_id: int
    seat_number: int

class RouteQuery(BaseModel):
    source: str
    destination: str

# Security
security = HTTPBearer()

def verify_admin_api_key(request: Request):
    api_key = request.headers.get("X-API-Key")
    if not api_key or api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return True

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=["HS256"])
        return payload
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

# Helper functions
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def create_token(user_id: int, username: str, is_admin: bool) -> str:
    payload = {
        "user_id": user_id,
        "username": username,
        "is_admin": is_admin,
        "exp": datetime.utcnow() + timedelta(days=1)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

# Routes
@app.post("/register")
async def register_user(user: UserCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        hashed_password = hash_password(user.password)
        cursor.execute(
            """
            INSERT INTO users (username, password, email, is_admin)
            VALUES (%s, %s, %s, %s)
            RETURNING id, username, email, is_admin
            """,
            (user.username, hashed_password, user.email, user.is_admin)
        )
        new_user = cursor.fetchone()
        conn.commit()
        return {"message": "User registered successfully", "user": new_user}
    except psycopg2.Error as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@app.post("/login")
async def login_user(user: UserLogin):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        hashed_password = hash_password(user.password)
        cursor.execute(
            "SELECT id, username, is_admin FROM users WHERE username = %s AND password = %s",
            (user.username, hashed_password)
        )
        user_data = cursor.fetchone()
        
        if not user_data:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        
        token = create_token(user_data["id"], user_data["username"], user_data["is_admin"])
        return {"token": token, "user": user_data}
    finally:
        cursor.close()
        conn.close()

@app.post("/trains", dependencies=[Depends(verify_admin_api_key)])
async def add_train(train: Train):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            """
            INSERT INTO trains (train_number, source, destination, total_seats)
            VALUES (%s, %s, %s, %s)
            RETURNING id, train_number, source, destination, total_seats
            """,
            (train.train_number, train.source, train.destination, train.total_seats)
        )
        new_train = cursor.fetchone()
        conn.commit()
        return {"message": "Train added successfully", "train": new_train}
    except psycopg2.Error as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cursor.close()
        conn.close()

@app.post("/availability")
async def get_seat_availability(route: RouteQuery):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            """
            SELECT t.*, 
                   (t.total_seats - COALESCE(COUNT(b.id), 0)) as available_seats
            FROM trains t
            LEFT JOIN bookings b ON t.id = b.train_id
            WHERE t.source = %s AND t.destination = %s
            GROUP BY t.id
            """,
            (route.source, route.destination)
        )
        trains = cursor.fetchall()
        return {"trains": trains}
    finally:
        cursor.close()
        conn.close()

@app.post("/bookings")
async def book_seat(booking: BookingCreate, user: dict = Depends(verify_token)):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Start transaction
        conn.autocommit = False
        
        # Check seat availability with row lock
        cursor.execute(
            """
            SELECT id, total_seats
            FROM trains
            WHERE id = %s
            FOR UPDATE
            """,
            (booking.train_id,)
        )
        train = cursor.fetchone()
        
        if not train:
            raise HTTPException(status_code=404, detail="Train not found")
        
        # Check if seat is already booked
        cursor.execute(
            """
            SELECT COUNT(*) as booked_seats
            FROM bookings
            WHERE train_id = %s
            """,
            (booking.train_id,)
        )
        booked_seats = cursor.fetchone()["booked_seats"]
        
        if booked_seats >= train["total_seats"]:
            raise HTTPException(status_code=400, detail="No seats available")
        
        # Create booking
        cursor.execute(
            """
            INSERT INTO bookings (user_id, train_id, seat_number, booking_date)
            VALUES (%s, %s, %s, NOW())
            RETURNING id, user_id, train_id, seat_number, booking_date
            """,
            (user["user_id"], booking.train_id, booking.seat_number)
        )
        new_booking = cursor.fetchone()
        
        conn.commit()
        return {"message": "Booking successful", "booking": new_booking}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.autocommit = True
        cursor.close()
        conn.close()

@app.get("/bookings/{booking_id}")
async def get_booking_details(booking_id: int, user: dict = Depends(verify_token)):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            """
            SELECT b.*, t.train_number, t.source, t.destination
            FROM bookings b
            JOIN trains t ON b.train_id = t.id
            WHERE b.id = %s AND b.user_id = %s
            """,
            (booking_id, user["user_id"])
        )
        booking = cursor.fetchone()
        
        if not booking:
            raise HTTPException(status_code=404, detail="Booking not found")
        
        return {"booking": booking}
    finally:
        cursor.close()
        conn.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)