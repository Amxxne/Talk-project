CC      = gcc
CFLAGS  = -Wall -Wextra -std=c11 -g
LDFLAGS = -lpthread

TARGET  = safesync-server
SRCS    = main.c client_handler.c
OBJS    = $(SRCS:.c=.o)

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(OBJS) -o $(TARGET) $(LDFLAGS)

%.o: %.c server.h
	$(CC) $(CFLAGS) -c $< -o $@

clean:
	rm -f $(OBJS) $(TARGET)

run: all
	./$(TARGET)

.PHONY: all clean run
