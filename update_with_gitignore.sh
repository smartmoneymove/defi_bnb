#!/bin/bash

# Скрипт для обновления репозитория с правильным применением .gitignore
# Использование: ./update_with_gitignore.sh

set -e  # Остановка при ошибке

echo "🔄 Обновление репозитория с применением нового .gitignore..."

# 1. Сохраняем текущие изменения (если есть)
if [[ -n $(git status --porcelain) ]]; then
    echo "📦 Сохраняем текущие изменения..."
    git add .
    git commit -m "Save current changes before gitignore update" || true
fi

# 2. Удаляем файлы из индекса, которые теперь игнорируются
echo "🧹 Очищаем кэш git от игнорируемых файлов..."

# Удаляем все файлы из индекса
git rm -r --cached . > /dev/null 2>&1 || true

# Добавляем обратно только те файлы, которые НЕ игнорируются
echo "➕ Добавляем обратно неигнорируемые файлы..."
git add .

# 3. Проверяем статус
echo "📋 Текущий статус после применения .gitignore:"
git status --short

# 4. Коммитим изменения .gitignore
if [[ -n $(git status --porcelain) ]]; then
    echo "💾 Коммитим обновления .gitignore..."
    git add .gitignore
    git commit -m "feat: update .gitignore with DeFi project structure

- Add crypto/blockchain specific ignores
- Better organize sections with headers  
- Add state files and trading data ignores
- Include wallet and keystore protections
- Add comprehensive OS and IDE coverage"
    
    echo "✅ Изменения закоммичены!"
else
    echo "ℹ️  Нет изменений для коммита"
fi

# 5. Подтягиваем последние изменения из удаленного репозитория
echo "⬇️  Подтягиваем изменения из удаленного репозитория..."

# Проверяем есть ли удаленный репозиторий
if git remote | grep -q origin; then
    # Получаем последние изменения
    git fetch origin
    
    # Получаем текущую ветку
    CURRENT_BRANCH=$(git branch --show-current)
    
    # Проверяем есть ли удаленная ветка
    if git branch -r | grep -q "origin/$CURRENT_BRANCH"; then
        echo "🔄 Мержим изменения из origin/$CURRENT_BRANCH..."
        git pull origin $CURRENT_BRANCH --no-edit
        echo "✅ Изменения успешно подтянуты!"
    else
        echo "⚠️  Удаленная ветка origin/$CURRENT_BRANCH не найдена"
        echo "📤 Пушим текущую ветку в origin..."
        git push -u origin $CURRENT_BRANCH
    fi
else
    echo "⚠️  Удаленный репозиторий не настроен"
fi

# 6. Показываем финальный статус
echo ""
echo "🎉 Обновление завершено!"
echo "📊 Финальный статус репозитория:"
git status --short

# 7. Показываем игнорируемые файлы для справки
echo ""
echo "🚫 Игнорируемые файлы в директории:"
git status --ignored --short | grep "!!" | head -10
if [[ $(git status --ignored --short | grep "!!" | wc -l) -gt 10 ]]; then
    echo "   ... и еще $(( $(git status --ignored --short | grep "!!" | wc -l) - 10 )) файлов"
fi

echo ""
echo "✨ Готово! Репозиторий обновлен с новым .gitignore" 