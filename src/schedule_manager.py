"""
Модуль для управления расписанием работы скрипта.
Обрабатывает файл schedule.json и определяет активные периоды работы в UTC.
"""
import json
import asyncio
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Tuple, Optional


class ScheduleManager:
    """Класс для управления расписанием работы скрипта"""
    
    def __init__(self, schedule_file_path: str):
        self.schedule_file_path = Path(schedule_file_path)
        self.schedule_data = self._load_schedule()
        self.close_script_path = Path(__file__).parent / "close_all_new.py"
    
    def _load_schedule(self) -> Dict:
        """Загружает расписание из JSON файла"""
        try:
            with open(self.schedule_file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"❌ Ошибка загрузки расписания: {e}")
            return {"liquidityScheduleUTC": {}}
    
    def _parse_time(self, time_str: str) -> Tuple[int, int]:
        """Парсит строку времени в часы и минуты"""
        hours, minutes = map(int, time_str.split(':'))
        return hours, minutes
    
    def _get_current_utc_time(self) -> datetime:
        """Возвращает текущее время в UTC"""
        return datetime.utcnow()
    
    def _time_to_minutes(self, hours: int, minutes: int) -> int:
        """Конвертирует время в минуты от начала дня"""
        return hours * 60 + minutes
    
    def _minutes_to_time_str(self, minutes: int) -> str:
        """Конвертирует минуты в строку времени"""
        hours = minutes // 60
        mins = minutes % 60
        return f"{hours:02d}:{mins:02d}"
    
    def get_day_schedule(self, day_name: str) -> List[Dict]:
        """Получает расписание для конкретного дня"""
        schedule = self.schedule_data.get("liquidityScheduleUTC", {})
        return schedule.get(day_name, [])
    
    def is_work_time(self, check_time: Optional[datetime] = None) -> bool:
        """
        Проверяет, является ли текущее время рабочим согласно расписанию.
        Учитывает переход через полночь (23:59 -> 00:00 следующего дня).
        """
        if check_time is None:
            check_time = self._get_current_utc_time()
        
        current_day = check_time.strftime("%A")
        current_minutes = self._time_to_minutes(check_time.hour, check_time.minute)
        
        # Проверяем текущий день
        day_schedule = self.get_day_schedule(current_day)
        for period in day_schedule:
            start_hours, start_minutes = self._parse_time(period["startUTC"])
            end_hours, end_minutes = self._parse_time(period["endUTC"])
            
            start_total_minutes = self._time_to_minutes(start_hours, start_minutes)
            end_total_minutes = self._time_to_minutes(end_hours, end_minutes)
            
            # Если период заканчивается в 23:59, проверяем переход на следующий день
            if end_hours == 23 and end_minutes == 59:
                if current_minutes >= start_total_minutes:
                    # Проверяем следующий день с 00:00 - если есть период начинающийся с 00:00, то это продолжение
                    next_day = (check_time + timedelta(days=1)).strftime("%A")
                    next_day_schedule = self.get_day_schedule(next_day)
                    for next_period in next_day_schedule:
                        next_start_hours, next_start_minutes = self._parse_time(next_period["startUTC"])
                        if next_start_hours == 0 and next_start_minutes == 0:
                            # Это продолжение периода с предыдущего дня
                            return True
                    # Если нет продолжения, период заканчивается в 23:59
                    return True
            else:
                # Обычная проверка в пределах одного дня
                if start_total_minutes <= current_minutes <= end_total_minutes:
                    return True
        
        # Проверяем, не является ли это продолжением периода с предыдущего дня
        prev_day = (check_time - timedelta(days=1)).strftime("%A")
        prev_day_schedule = self.get_day_schedule(prev_day)
        for period in prev_day_schedule:
            end_hours, end_minutes = self._parse_time(period["endUTC"])
            if end_hours == 23 and end_minutes == 59:
                # Период с предыдущего дня мог продолжиться на текущий день
                # Проверяем, есть ли период с 00:00 в текущем дне
                for current_period in day_schedule:
                    curr_start_hours, curr_start_minutes = self._parse_time(current_period["startUTC"])
                    curr_end_hours, curr_end_minutes = self._parse_time(current_period["endUTC"])
                    
                    if curr_start_hours == 0 and curr_start_minutes == 0:
                        curr_end_total_minutes = self._time_to_minutes(curr_end_hours, curr_end_minutes)
                        if current_minutes <= curr_end_total_minutes:
                            return True
        
        return False
    
    def get_next_work_start(self, from_time: Optional[datetime] = None) -> Optional[datetime]:
        """
        Находит время следующего начала работы.
        Возвращает None, если нет следующих периодов в расписании.
        """
        if from_time is None:
            from_time = self._get_current_utc_time()
        
        # Ищем в ближайшие 7 дней
        for days_ahead in range(8):
            check_date = from_time + timedelta(days=days_ahead)
            day_name = check_date.strftime("%A")
            day_schedule = self.get_day_schedule(day_name)
            
            for period in day_schedule:
                start_hours, start_minutes = self._parse_time(period["startUTC"])
                
                # Создаем datetime для начала периода
                period_start = check_date.replace(
                    hour=start_hours, 
                    minute=start_minutes, 
                    second=0, 
                    microsecond=0
                )
                
                # Если это текущий день, проверяем что время еще не прошло
                if days_ahead == 0:
                    if period_start > from_time:
                        return period_start
                else:
                    return period_start
        
        return None
    
    def get_current_work_end(self, from_time: Optional[datetime] = None) -> Optional[datetime]:
        """
        Находит время окончания текущего рабочего периода.
        Возвращает None, если сейчас не рабочее время.
        """
        if from_time is None:
            from_time = self._get_current_utc_time()
        
        if not self.is_work_time(from_time):
            return None
        
        current_day = from_time.strftime("%A")
        current_minutes = self._time_to_minutes(from_time.hour, from_time.minute)
        
        # Проверяем текущий день
        day_schedule = self.get_day_schedule(current_day)
        for period in day_schedule:
            start_hours, start_minutes = self._parse_time(period["startUTC"])
            end_hours, end_minutes = self._parse_time(period["endUTC"])
            
            start_total_minutes = self._time_to_minutes(start_hours, start_minutes)
            end_total_minutes = self._time_to_minutes(end_hours, end_minutes)
            
            # Если период заканчивается в 23:59, ищем продолжение на следующий день
            if end_hours == 23 and end_minutes == 59:
                if current_minutes >= start_total_minutes:
                    # Ищем продолжение на следующий день
                    next_day = (from_time + timedelta(days=1)).strftime("%A")
                    next_day_schedule = self.get_day_schedule(next_day)
                    for next_period in next_day_schedule:
                        next_start_hours, next_start_minutes = self._parse_time(next_period["startUTC"])
                        if next_start_hours == 0 and next_start_minutes == 0:
                            # Это продолжение, находим реальное окончание
                            next_end_hours, next_end_minutes = self._parse_time(next_period["endUTC"])
                            next_day_date = from_time + timedelta(days=1)
                            return next_day_date.replace(
                                hour=next_end_hours,
                                minute=next_end_minutes,
                                second=0,
                                microsecond=0
                            )
                    # Если нет продолжения, заканчиваем в 23:59 текущего дня
                    return from_time.replace(hour=23, minute=59, second=0, microsecond=0)
            else:
                # Обычное окончание в пределах дня
                if start_total_minutes <= current_minutes <= end_total_minutes:
                    return from_time.replace(
                        hour=end_hours,
                        minute=end_minutes,
                        second=0,
                        microsecond=0
                    )
        
        # Проверяем, не продолжение ли это с предыдущего дня
        prev_day = (from_time - timedelta(days=1)).strftime("%A")
        prev_day_schedule = self.get_day_schedule(prev_day)
        for period in prev_day_schedule:
            end_hours, end_minutes = self._parse_time(period["endUTC"])
            if end_hours == 23 and end_minutes == 59:
                # Ищем соответствующий период в текущем дне
                for current_period in day_schedule:
                    curr_start_hours, curr_start_minutes = self._parse_time(current_period["startUTC"])
                    curr_end_hours, curr_end_minutes = self._parse_time(current_period["endUTC"])
                    
                    if curr_start_hours == 0 and curr_start_minutes == 0:
                        curr_end_total_minutes = self._time_to_minutes(curr_end_hours, curr_end_minutes)
                        if current_minutes <= curr_end_total_minutes:
                            return from_time.replace(
                                hour=curr_end_hours,
                                minute=curr_end_minutes,
                                second=0,
                                microsecond=0
                            )
        
        return None
    
    async def close_all_positions(self):
        """Запускает скрипт закрытия всех позиций"""
        try:
            print("🔄 Запуск скрипта закрытия позиций...")
            result = subprocess.run([
                "python3", str(self.close_script_path)
            ], capture_output=True, text=True, cwd=self.close_script_path.parent)
            
            if result.returncode == 0:
                print("✅ Все позиции успешно закрыты")
                if result.stdout:
                    print(f"Вывод скрипта:\n{result.stdout}")
            else:
                print(f"❌ Ошибка при закрытии позиций (код: {result.returncode})")
                if result.stderr:
                    print(f"Ошибка: {result.stderr}")
                if result.stdout:
                    print(f"Вывод: {result.stdout}")
                    
        except Exception as e:
            print(f"❌ Критическая ошибка при запуске скрипта закрытия: {e}")
    
    def format_time_until(self, target_time: datetime) -> str:
        """Форматирует время до целевого момента"""
        now = self._get_current_utc_time()
        if target_time <= now:
            return "сейчас"
        
        delta = target_time - now
        total_seconds = int(delta.total_seconds())
        
        days = total_seconds // 86400
        hours = (total_seconds % 86400) // 3600
        minutes = (total_seconds % 3600) // 60
        
        parts = []
        if days > 0:
            parts.append(f"{days}д")
        if hours > 0:
            parts.append(f"{hours}ч")
        if minutes > 0:
            parts.append(f"{minutes}м")
        
        if not parts:
            return "менее минуты"
        
        return " ".join(parts)
    
    def print_schedule_status(self):
        """Выводит текущий статус расписания"""
        now = self._get_current_utc_time()
        is_work = self.is_work_time(now)
        
        print(f"\n=== СТАТУС РАСПИСАНИЯ (UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}) ===")
        
        if is_work:
            work_end = self.get_current_work_end(now)
            if work_end:
                time_left = self.format_time_until(work_end)
                print(f"🟢 РАБОЧЕЕ ВРЕМЯ")
                print(f"⏰ Окончание работы: {work_end.strftime('%Y-%m-%d %H:%M UTC')} (через {time_left})")
            else:
                print(f"🟢 РАБОЧЕЕ ВРЕМЯ (окончание не определено)")
        else:
            next_start = self.get_next_work_start(now)
            if next_start:
                time_until = self.format_time_until(next_start)
                print(f"🔴 ВРЕМЯ ОЖИДАНИЯ")
                print(f"⏰ Следующий запуск: {next_start.strftime('%Y-%m-%d %H:%M UTC')} (через {time_until})")
            else:
                print(f"🔴 НЕТ ЗАПЛАНИРОВАННЫХ ПЕРИОДОВ РАБОТЫ")


def get_run_mode_choice() -> str:
    """Запрашивает у пользователя режим запуска"""
    print("\n=== РЕЖИМ ЗАПУСКА ===")
    print("1. Запустить сейчас (игнорировать расписание)")
    print("2. Работать по расписанию")
    
    while True:
        try:
            choice = input("\nВыберите режим (1 или 2): ").strip()
            if choice == "1":
                return "immediate"
            elif choice == "2":
                return "scheduled"
            else:
                print("❌ Неверный выбор. Введите 1 или 2.")
        except KeyboardInterrupt:
            print("\nПрограмма прервана пользователем.")
            exit(0)
