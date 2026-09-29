import os

# Tạo ra database cơ bản để demo, không gọi mạng, luôn chạy được và mỗi lần chiếu đều ra cùng một kết quả.
# Dữ liệu này dùng cho tool đặt vé máy bay, không dùng cho tool du lịch
# Sẽ có 3 hãng máy bay, Vietnam airlines mắc nhất, Vietjet rẻ nhất, Bamboo Airways trung bình
VE_MAY_BAY = [
    dict(Airline="Vietnam Airlines", Departure="Hà Nội", Arrival="Đà Nẵng", Date="2024-06-01", Hour="08:00", Price=300, State="available"),
    dict(Airline="Vietnam Airlines", Departure="Hà Nội", Arrival="Đà Nẵng", Date="2024-06-02", Hour="12:00", Price=320, State="full"),
    dict(Airline="Vietnam Airlines", Departure="Hà Nội", Arrival="Đà Nẵng", Date="2024-06-03", Hour="16:00", Price=350, State="available"),
    dict(Airline="Vietjet", Departure="Hà Nội", Arrival="Đà Nẵng", Date="2024-06-01", Hour="09:00", Price=200, State="full"),
    dict(Airline="Vietjet", Departure="Hà Nội", Arrival="Đà Nẵng", Date="2024-06-02", Hour="13:00", Price=220, State="available"),
    dict(Airline="Vietjet", Departure="Hà Nội", Arrival="Đà Nẵng", Date="2024-06-03", Hour="17:00", Price=250, State="available"),
    dict(Airline="Bamboo Airways", Departure="Hà Nội", Arrival="Đà Nẵng", Date="2024-06-01", Hour="10:00", Price=250, State="full"),
    dict(Airline="Bamboo Airways", Departure="Hà Nội", Arrival="Đà Nẵng", Date="2024-06-02", Hour="14:00", Price=270, State="available"),
    dict(Airline="Bamboo Airways", Departure="Hà Nội", Arrival="Đà Nẵng", Date="2024-06-03", Hour="18:00", Price=300, State="full"),

    # Scenario "chon-loc": ve re nhat da het cho; agent phai doc observation
    # va chon Bamboo (240) thay vi Vietjet (180, full) hay Vietnam Airlines (320).
    dict(Airline="Vietjet", Departure="TP.HCM", Arrival="Nha Trang", Date="2024-07-01", Hour="06:00", Price=180, State="full"),
    dict(Airline="Bamboo Airways", Departure="TP.HCM", Arrival="Nha Trang", Date="2024-07-01", Hour="09:00", Price=240, State="available"),
    dict(Airline="Vietnam Airlines", Departure="TP.HCM", Arrival="Nha Trang", Date="2024-07-01", Hour="11:00", Price=320, State="available"),

    # Scenario "khong-co-ngay": ngay user hoi khong ton tai. Du lieu chi co ngay
    # ke tiep, de model co the doi chien luoc hoac bi harness bat retry lap.
    dict(Airline="Vietnam Airlines", Departure="Hà Nội", Arrival="Huế", Date="2024-07-02", Hour="07:30", Price=280, State="available"),
    dict(Airline="Vietjet", Departure="Hà Nội", Arrival="Huế", Date="2024-07-02", Hour="13:20", Price=190, State="available"),
]

def _chuan(s: str) -> str:
    return " ".join(s.lower().split())

def search_flight_info(departure: str, arrival: str, date: str, hour: str = None) -> list:
    """Tìm chuyến bay theo từ khoá. Trả về danh sách chuyến bay kèm thông tin chi tiết.

    Dùng tool này TRƯỚC khi hỏi đặt vé, để lấy tên điểm đến hợp lệ.
    """
    dep = _chuan(departure)
    arr = _chuan(arrival)
    hour = _chuan(hour) if hour else None
    hits = [d for d in VE_MAY_BAY
            if _chuan(d["Departure"]) == dep and _chuan(d["Arrival"]) == arr and d["Date"] == date and (hour is None or _chuan(d["Hour"]) == hour)]
    return hits

def book_flight(airline: str, departure: str, arrival: str, date: str, hour: str) -> dict:
    """Đặt vé cho chuyến bay đã chọn. Trả về thông tin đặt vé hoặc lỗi nếu không tìm thấy chuyến bay.

    Dùng tool này SAU khi tìm chuyến bay, để đặt vé cho chuyến bay hợp lệ.
    """
    dep = _chuan(departure)
    arr = _chuan(arrival)
    airline = _chuan(airline)
    hour = _chuan(hour)
    for d in VE_MAY_BAY:
        if (_chuan(d["Airline"]) == airline and _chuan(d["Departure"]) == dep and
            _chuan(d["Arrival"]) == arr and d["Date"] == date and _chuan(d["Hour"]) == hour):
            if d["State"] == "available":
                return {"status": "success", "message": f"Đặt vé thành công cho chuyến bay {airline} từ {departure} đến {arrival} vào {date} lúc {hour}."}
            else:
                return {"status": "error", "message": "Chuyến bay đã hết chỗ hoặc không khả dụng.", "details": d, "hint": "Gọi search_flight_info() để tìm chuyến bay khác."}
    return {"status": "error", "message": "Không tìm thấy chuyến bay phù hợp.", "hint": "Gọi search_flight_info() để tìm hãng airline hoặc chuyến bay khác."}

def tool_langchain():
    """Trả về danh sách các tool có sẵn cho LangChain."""
    from langchain_core.tools import tool
    return [tool(search_flight_info, name="search_flight_info", description="Tìm chuyến bay theo từ khoá. Trả về danh sách chuyến bay kèm thông tin chi tiết."),
            tool(book_flight, name="book_flight", description="Đặt vé cho chuyến bay đã chọn. Trả về thông tin đặt vé hoặc lỗi nếu không tìm thấy chuyến bay.")]
  
if __name__ == "__main__":
    # Ví dụ chạy thử
    print(search_flight_info("Hà Nội", "Đà Nẵng", "2024-06-01"))
    print(book_flight("Vietjet", "Hà Nội", "Đà Nẵng", "2024-06-01", "09:00"))
    print(book_flight("Vietjet", "Hà Nội", "Đà Nẵng", "2024-06-01", "13:00"))  # full
    print(book_flight("Vietnam Airlines", "Hà Nội", "Đà Nẵng", "2024-06-01", "08:00"))   
