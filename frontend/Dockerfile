FROM node:20-slim

WORKDIR /app

# Copy package files
COPY package.json package-lock.json ./

# Install dependencies
RUN npm ci

# Copy app code
COPY . .

# Expose port
EXPOSE 5173

# Run dev server
CMD ["npm", "run", "dev", "--", "--host"]
